"""S3 keys, CloudFront signed GET, and historic Cloudinary playback URLs."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.api.videos import _original_video_url
from app.services import s3_service
from app.services.s3_service import PLAYBACK_TRANSFORMATION, legacy_cloudinary_playback_url

MOV = "https://res.cloudinary.com/demo/video/upload/v1700000000/criclab/incoming/clip.mov"
MP4 = (
    "https://res.cloudinary.com/demo/video/upload/"
    f"{PLAYBACK_TRANSFORMATION}/v1700000000/criclab/incoming/clip.mp4"
)


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


class _CfSettings:
    s3_bucket = "criclab-s3-bucket"
    s3_region = "ap-south-1"
    aws_region = "us-east-1"
    aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"
    aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    cloudfront_domain = "d111111abcdef8.cloudfront.net"
    cloudfront_key_pair_id = "KTESTPAIR"
    cloudfront_private_key = ""


class ObjectKeyTests(unittest.TestCase):
    def test_allowed_prefixes(self) -> None:
        self.assertTrue(s3_service.is_our_object_key("original/u1/a.mp4"))
        self.assertTrue(s3_service.is_our_object_key("compressed/vid.mp4"))
        self.assertTrue(s3_service.is_our_object_key("overlays/job_overlay.mp4"))
        self.assertTrue(s3_service.is_our_object_key("files/job_report.pdf"))

    def test_rejects_other_prefixes(self) -> None:
        self.assertFalse(s3_service.is_our_object_key("criclab/incoming/a.mp4"))
        self.assertFalse(s3_service.is_our_object_key("../original/a.mp4"))
        self.assertFalse(s3_service.is_our_object_key(""))

    def test_regional_s3_endpoint(self) -> None:
        self.assertEqual(
            s3_service.s3_endpoint_url("ap-south-1"),
            "https://s3.ap-south-1.amazonaws.com",
        )
        self.assertIsNone(s3_service.s3_endpoint_url(""))

    def test_presigned_put_is_unsigned_payload_on_regional_host(self) -> None:
        settings = _CfSettings()
        with patch("app.services.s3_service.get_settings", return_value=settings):
            params = s3_service.presigned_put("original/u1/a.mp4", "video/mp4")
        assert params is not None
        url = params["upload_url"]
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "criclab-s3-bucket.s3.ap-south-1.amazonaws.com")
        self.assertEqual(parsed.path, "/original/u1/a.mp4")
        self.assertEqual(query.get("X-Amz-SignedHeaders"), ["host"])
        self.assertEqual(query.get("X-Amz-Content-Sha256"), ["UNSIGNED-PAYLOAD"])
        self.assertTrue(query.get("X-Amz-Signature"))
        self.assertEqual(params["headers"], {})

    def test_incoming_original_key_from_s3_url(self) -> None:
        settings = _CfSettings()
        with patch("app.services.s3_service.get_settings", return_value=settings):
            self.assertEqual(
                s3_service.incoming_original_key("original/u1/a.mov"),
                "original/u1/a.mov",
            )
            self.assertEqual(
                s3_service.incoming_original_key(
                    "https://criclab-s3-bucket.s3.ap-south-1.amazonaws.com/original/u1/a.mov"
                ),
                "original/u1/a.mov",
            )
            self.assertEqual(
                s3_service.incoming_original_key(
                    "https://s3.ap-south-1.amazonaws.com/criclab-s3-bucket/original/u1/a.mov"
                ),
                "original/u1/a.mov",
            )
            self.assertIsNone(
                s3_service.incoming_original_key(
                    "https://d111111abcdef8.cloudfront.net/original/u1/a.mov?Expires=1&Signature=x&Key-Pair-Id=KTESTPAIR"
                )
            )
            self.assertIsNone(s3_service.incoming_original_key("https://res.cloudinary.com/demo/video/upload/a.mp4"))


class LegacyCloudinaryPlaybackTests(unittest.TestCase):
    def test_iphone_mov_becomes_h264_mp4(self) -> None:
        out = legacy_cloudinary_playback_url(MOV)
        self.assertIn(PLAYBACK_TRANSFORMATION, out)
        self.assertTrue(out.endswith(".mp4"))
        self.assertEqual(out, MP4)

    def test_already_transformed_url_is_left_alone(self) -> None:
        self.assertEqual(legacy_cloudinary_playback_url(MP4), MP4)

    def test_non_cloudinary_url_is_left_alone(self) -> None:
        local = "/media/videos/vid_abc.mp4"
        self.assertEqual(legacy_cloudinary_playback_url(local), local)


class SignedGetTests(unittest.TestCase):
    def setUp(self) -> None:
        s3_service._private_key.cache_clear()
        self.settings = _CfSettings()
        self.settings.cloudfront_private_key = _pem().replace("\n", "\\n")

    def test_signed_get_has_cloudfront_query(self) -> None:
        with patch("app.services.s3_service.get_settings", return_value=self.settings):
            url = s3_service.signed_get("overlays/job_overlay.mp4")
        self.assertIsNotNone(url)
        assert url is not None
        self.assertTrue(url.startswith("https://d111111abcdef8.cloudfront.net/overlays/job_overlay.mp4?"))
        self.assertIn("Expires=", url)
        self.assertIn("Signature=", url)
        self.assertIn("Key-Pair-Id=KTESTPAIR", url)

    def test_signed_get_rejects_foreign_key(self) -> None:
        with patch("app.services.s3_service.get_settings", return_value=self.settings):
            self.assertIsNone(s3_service.signed_get("other/secret.mp4"))

    def test_pem_bytes_expands_escaped_newlines(self) -> None:
        pem = s3_service.pem_bytes(self.settings.cloudfront_private_key)
        self.assertIn(b"BEGIN", pem)
        self.assertIn(b"\n", pem)
        self.assertNotIn(b"\\n", pem)


class OriginalVideoUrlTests(unittest.TestCase):
    def test_compressed_key_wins(self) -> None:
        video = {
            "compressed_key": "compressed/vid_abc.mp4",
            "source_url": MOV,
            "path": "/Users/macbookpro/.local/share/criclab/videos/vid_abc.mov",
        }
        with patch("app.api.videos.s3_service.signed_get", return_value="https://cdn.example/clip.mp4"):
            self.assertEqual(_original_video_url(video), "https://cdn.example/clip.mp4")

    def test_cloudinary_source_wins_over_missing_mac_path(self) -> None:
        video = {
            "source_url": MOV,
            "path": "/Users/macbookpro/.local/share/criclab/videos/vid_abc.mov",
        }
        with patch("app.api.videos.s3_service.signed_get", return_value=None):
            self.assertEqual(_original_video_url(video), MP4)

    def test_missing_source_and_missing_file_is_none(self) -> None:
        video = {"source_url": None, "path": "/no/such/vid_abc.mov"}
        with patch("app.api.videos.s3_service.signed_get", return_value=None):
            self.assertIsNone(_original_video_url(video))

    def test_local_file_when_source_url_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            videos = Path(tmp) / "videos"
            videos.mkdir()
            clip = videos / "vid_local.mp4"
            clip.write_bytes(b"not-a-real-mp4")
            video = {"source_url": None, "path": str(clip)}

            class _Settings:
                storage_path = Path(tmp)

            with (
                patch("app.api.videos.s3_service.signed_get", return_value=None),
                patch("app.api.videos.get_settings", return_value=_Settings()),
            ):
                self.assertEqual(_original_video_url(video), "/media/videos/vid_local.mp4")


if __name__ == "__main__":
    unittest.main()
