"""Report storage adapters selected by configuration.

``file`` keeps the existing local markdown tree. ``s3`` / ``minio`` uploads the
same tree (plus a zip) to an S3-compatible bucket and returns presigned GET URLs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from zipfile import ZIP_DEFLATED, ZipFile


class ReportStoreError(ValueError):
    """Invalid report-store configuration or destination."""


@dataclass(frozen=True)
class StoredArtifact:
    name: str
    key: str
    uri: str | None
    content_type: str | None = None


@dataclass(frozen=True)
class ReportStoreResult:
    backend: str
    destination: str
    artifacts: tuple[StoredArtifact, ...]
    bucket: str | None = None

    def artifact(self, name: str) -> StoredArtifact | None:
        for item in self.artifacts:
            if item.name == name:
                return item
        return None

    @property
    def complete_report_path(self) -> Path | None:
        if self.backend != "file":
            return None
        item = self.artifact("complete_report.md")
        return Path(item.uri) if item and item.uri else None

    @property
    def download_url(self) -> str | None:
        if self.backend != "s3":
            return None
        item = self.artifact("report.zip") or self.artifact("complete_report.md")
        return item.uri if item else None

    def local_path(self, name: str) -> Path | None:
        if self.backend != "file":
            return None
        item = self.artifact(name)
        return Path(item.uri) if item and item.uri else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "bucket": self.bucket,
            "destination": self.destination,
            "download_url": self.download_url,
            "artifacts": [
                {"name": item.name, "key": item.key, "url": item.uri}
                for item in self.artifacts
            ],
        }


class ReportStoreAdapter(Protocol):
    backend: str

    def save(
        self, files: Mapping[str, str], *, destination: str | Path
    ) -> ReportStoreResult: ...


def _normalise_relative_parts(relative: str) -> Path:
    path = Path(str(relative).replace("\\", "/"))
    if path.is_absolute() or path.anchor:
        raise ReportStoreError(f"report path must be relative: {relative}")
    if ".." in path.parts:
        raise ReportStoreError(f"report path cannot traverse parents: {relative}")
    if not path.parts or path.parts == (".",):
        raise ReportStoreError("report path cannot be empty")
    return path


def _content_type(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def _zip_tree(files: Mapping[str, str]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()


class FileReportStore:
    """Canonical copy is a local directory tree."""

    backend = "file"

    def save(
        self, files: Mapping[str, str], *, destination: str | Path
    ) -> ReportStoreResult:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        artifacts: list[StoredArtifact] = []
        for name, content in files.items():
            relative = _normalise_relative_parts(name)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            artifacts.append(
                StoredArtifact(
                    name=name,
                    key=relative.as_posix(),
                    uri=str(path),
                    content_type=_content_type(name),
                )
            )
        return ReportStoreResult(
            backend=self.backend,
            destination=str(root),
            artifacts=tuple(artifacts),
        )


class S3ReportStore:
    """Canonical copy is an S3/MinIO prefix; download via presigned GET."""

    backend = "s3"

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str = "tradingagents/reports",
        presign_seconds: int = 604800,
    ):
        if not bucket:
            raise ReportStoreError("s3_bucket is required for the S3 report store")
        self.client = client
        self.bucket = bucket
        self.prefix = str(prefix or "").strip("/")
        self.presign_seconds = int(presign_seconds)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, client: Any | None = None):
        bucket = str(config.get("s3_bucket") or "").strip()
        if not bucket:
            raise ReportStoreError("s3_bucket is required for the S3 report store")
        return cls(
            client=client or _build_s3_client(config),
            bucket=bucket,
            prefix=str(config.get("s3_prefix") or "tradingagents/reports"),
            presign_seconds=int(config.get("s3_presign_seconds") or 604800),
        )

    def _destination_prefix(self, destination: str | Path) -> str:
        raw = str(destination).replace("\\", "/")
        path = Path(raw)
        if path.is_absolute() or raw.startswith("/"):
            raise ReportStoreError(
                "S3 destination must be a relative object prefix, not an absolute path"
            )
        relative = _normalise_relative_parts(raw)
        if self.prefix:
            return f"{self.prefix}/{relative.as_posix()}"
        return relative.as_posix()

    def _object_key(self, prefix: str, name: str) -> str:
        relative = _normalise_relative_parts(name)
        return f"{prefix}/{relative.as_posix()}"

    def _presign(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presign_seconds,
        )

    def save(
        self, files: Mapping[str, str], *, destination: str | Path
    ) -> ReportStoreResult:
        prefix = self._destination_prefix(destination)
        payload = dict(files)
        planned_names = list(payload) + ["report.zip"]
        planned_keys = {name: self._object_key(prefix, name) for name in planned_names}
        planned_urls = {name: self._presign(key) for name, key in planned_keys.items()}
        if "provenance.json" in payload:
            try:
                provenance = json.loads(payload["provenance.json"])
            except json.JSONDecodeError:
                provenance = None
            if isinstance(provenance, dict):
                provenance = {
                    **provenance,
                    "storage": {
                        "backend": self.backend,
                        "bucket": self.bucket,
                        "destination": prefix,
                        "download_url": planned_urls["report.zip"],
                        "artifacts": [
                            {
                                "name": name,
                                "key": planned_keys[name],
                                "url": planned_urls[name],
                            }
                            for name in planned_names
                        ],
                    },
                }
                payload["provenance.json"] = json.dumps(
                    provenance, ensure_ascii=False, indent=2, default=str
                )

        artifacts: list[StoredArtifact] = []
        for name, content in payload.items():
            key = planned_keys[name]
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType=_content_type(name),
            )
            artifacts.append(
                StoredArtifact(
                    name=name,
                    key=key,
                    uri=planned_urls[name],
                    content_type=_content_type(name),
                )
            )

        zip_key = planned_keys["report.zip"]
        self.client.put_object(
            Bucket=self.bucket,
            Key=zip_key,
            Body=_zip_tree(payload),
            ContentType=_content_type("report.zip"),
            ContentDisposition='attachment; filename="report.zip"',
        )
        artifacts.append(
            StoredArtifact(
                name="report.zip",
                key=zip_key,
                uri=planned_urls["report.zip"],
                content_type=_content_type("report.zip"),
            )
        )
        return ReportStoreResult(
            backend=self.backend,
            destination=prefix,
            artifacts=tuple(artifacts),
            bucket=self.bucket,
        )


def _build_s3_client(config: Mapping[str, Any]) -> Any:
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ReportStoreError(
            "S3 report store requires boto3; install with pip install 'tradingagents[s3]'"
        ) from exc
    endpoint = config.get("s3_endpoint_url")
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        aws_access_key_id=config.get("s3_access_key") or None,
        aws_secret_access_key=config.get("s3_secret_key") or None,
        region_name=str(config.get("s3_region") or "us-east-1"),
        config=BotoConfig(s3={"addressing_style": "path" if endpoint else "auto"}),
    )


def build_report_store(
    config: Mapping[str, Any] | None = None, *, client: Any | None = None
) -> ReportStoreAdapter:
    config = config or {}
    backend = str(config.get("report_store") or "file").strip().lower().replace("-", "_")
    if backend in {"file", "local", "filesystem"}:
        return FileReportStore()
    if backend in {"s3", "minio"}:
        return S3ReportStore.from_config(config, client=client)
    raise ReportStoreError(
        f"unknown report_store {backend!r}; expected 'file' or 's3'"
    )
