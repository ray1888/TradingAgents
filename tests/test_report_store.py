"""Report storage adapters: local files vs S3/MinIO, selected by config."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from tradingagents.report_store import (
    FileReportStore,
    ReportStoreError,
    S3ReportStore,
    build_report_store,
)


class FakeS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        body = kwargs["Body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif hasattr(body, "read"):
            body = body.read()
        self.objects[(bucket, key)] = bytes(body)
        self.put_calls.append(kwargs)
        return {}

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        assert ClientMethod == "get_object"
        return (
            f"https://minio.test/{Params['Bucket']}/{Params['Key']}"
            f"?expires={ExpiresIn}"
        )


def _tree():
    return {
        "complete_report.md": "# Report\n",
        "provenance.json": '{"status": "success"}',
        "1_analysts/market.md": "MKT",
    }


@pytest.mark.unit
def test_file_adapter_writes_relative_tree(tmp_path):
    result = FileReportStore().save(_tree(), destination=tmp_path)

    assert result.backend == "file"
    assert (tmp_path / "complete_report.md").read_text(encoding="utf-8") == "# Report\n"
    assert (tmp_path / "1_analysts" / "market.md").read_text(encoding="utf-8") == "MKT"
    assert result.complete_report_path == tmp_path / "complete_report.md"
    assert result.download_url is None
    assert result.as_dict()["download_url"] is None
    assert result.as_dict()["artifacts"][0]["url"].endswith("complete_report.md")


@pytest.mark.unit
def test_s3_adapter_uploads_tree_and_zip_with_presigned_urls():
    client = FakeS3Client()
    store = S3ReportStore(
        client=client,
        bucket="tradingagents",
        prefix="tradingagents/reports",
        presign_seconds=3600,
    )

    result = store.save(_tree(), destination="600519.SH-20260908-014840")

    assert result.backend == "s3"
    assert result.download_url.endswith("report.zip?expires=3600")
    report_key = "tradingagents/reports/600519.SH-20260908-014840/complete_report.md"
    zip_key = "tradingagents/reports/600519.SH-20260908-014840/report.zip"
    assert client.objects[(store.bucket, report_key)].decode() == "# Report\n"
    zip_bytes = client.objects[(store.bucket, zip_key)]
    with ZipFile(BytesIO(zip_bytes)) as archive:
        assert archive.read("complete_report.md").decode() == "# Report\n"
        assert "1_analysts/market.md" in archive.namelist()
    payload = result.as_dict()
    assert payload["bucket"] == "tradingagents"
    assert "secret" not in str(payload).lower()
    assert payload["download_url"] == result.download_url


@pytest.mark.unit
def test_s3_adapter_rejects_absolute_destination_as_object_key():
    store = S3ReportStore(
        client=FakeS3Client(),
        bucket="tradingagents",
        prefix="tradingagents/reports",
    )
    with pytest.raises(ReportStoreError, match="absolute"):
        store.save(_tree(), destination="/home/appuser/.tradingagents/logs/run")


@pytest.mark.unit
def test_factory_defaults_to_file_store():
    store = build_report_store({})
    assert isinstance(store, FileReportStore)
    assert build_report_store({"report_store": "file"}).backend == "file"


@pytest.mark.unit
def test_factory_builds_s3_store_from_config():
    store = build_report_store(
        {
            "report_store": "s3",
            "s3_endpoint_url": "http://minio:9000",
            "s3_bucket": "tradingagents",
            "s3_prefix": "tradingagents/reports",
            "s3_access_key": "key",
            "s3_secret_key": "secret",
            "s3_presign_seconds": 120,
        },
        client=FakeS3Client(),
    )
    assert isinstance(store, S3ReportStore)
    assert store.backend == "s3"
    assert store.bucket == "tradingagents"
    assert store.presign_seconds == 120


@pytest.mark.unit
def test_factory_requires_bucket_for_s3():
    with pytest.raises(ReportStoreError, match="s3_bucket"):
        build_report_store({"report_store": "minio", "s3_endpoint_url": "http://minio:9000"})
