import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastfiles.access import new_computer_permission
from fastfiles.activity import ActivityStore
from fastfiles.availability import check_peers, probe_peer
from fastfiles.locker import (
    Locker,
    LockerConfig,
    LockerHTTPServer,
    LockerRequestHandler,
    LockerService,
    Peer,
    PeerClient,
    TransferCancelled,
    hash_access_code,
    parse_peer_address,
)
from fastfiles.transfers import copy_from_peer, copy_to_peer


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "locker"
        self.root.mkdir()

    def test_empty_allow_shares_nothing(self):
        (self.root / "file").write_text("private")
        locker = Locker(self.root, [])
        self.assertEqual(locker.list(), [])
        with self.assertRaises(PermissionError):
            locker.resolve("file")

    def test_deny_applies_to_root_files_and_directory_descendants(self):
        (self.root / "private").mkdir()
        (self.root / "private" / "data").write_text("private")
        (self.root / "id.key").write_text("private")
        locker = Locker(self.root, ["**"], ["private", "**/*.key"])
        self.assertEqual(locker.list(), [])
        for path in ("private/data", "private/new/data", "id.key"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                locker.resolve(path, must_exist=False)

    def test_symlinks_and_temporary_files_are_not_shared(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_text("private")
        try:
            (self.root / "link").symlink_to(outside)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows symlinks require Developer Mode or administrator privileges")
            raise
        (self.root / ".fastfiles-working").write_text("unfinished")
        locker = Locker(self.root)
        self.assertEqual(locker.list(), [])
        for path in ("link", ".fastfiles-working"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                locker.resolve(path)

    def test_allow_double_star_includes_its_parent(self):
        (self.root / "public").mkdir()
        self.assertEqual([entry["name"] for entry in Locker(self.root, ["public/**"]).list()], ["public"])

    def test_config_errors_never_replace_existing_policy(self):
        path = Path(self.temp.name) / "config.json"
        for contents in ('{"allow_patterns":', "[]", '{"device_id": 4}'):
            path.write_text(contents)
            with self.assertRaises(ValueError):
                LockerConfig.load(path)
            self.assertEqual(path.read_text(), contents)

    def test_config_validates_types_and_is_private(self):
        path = Path(self.temp.name) / "config.json"
        config = LockerConfig.load(path)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for field, invalid in (
            ("allow_patterns", "**"),
            ("deny_patterns", None),
            ("read_only", "false"),
            ("port", "22"),
        ):
            data = json.loads(path.read_text())
            data[field] = invalid
            path.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(ValueError):
                LockerConfig.load(path)
            config.save(path)

    def test_peer_addresses_reject_downgrades_credentials_and_ambiguous_paths(self):
        for address in (
            "https://server",
            "http://user:pass@server",
            "server:0",
            "server:65536",
            "server/path",
            "server?x=y",
            "bad host",
            "[not-ip]",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                parse_peer_address(address)

    def test_discovery_advertises_the_bound_address(self):
        config = LockerConfig(
            "test-discovery",
            "Test",
            str(self.root),
            hash_access_code("123456"),
            "123456",
            bind_address="127.0.0.1",
            port=0,
        )
        service = LockerService(config)
        try:
            with patch("zeroconf.Zeroconf") as zeroconf:
                service.start()
                info = zeroconf.return_value.register_service.call_args.args[0]
                self.assertEqual(info.parsed_addresses(), ["127.0.0.1"])
                self.assertEqual(info.port, service.port)
        finally:
            service.stop()

    @unittest.skipUnless(socket.has_ipv6, "IPv6 unavailable")
    def test_explicit_ipv6_service_can_be_reached(self):
        config = LockerConfig(
            "test-ipv6",
            "IPv6",
            str(self.root),
            hash_access_code("123456"),
            "123456",
            bind_address="::1",
            port=0,
        )
        try:
            service = LockerService(config)
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        try:
            service.start(advertise=False)
            peer = parse_peer_address(f"[::1]:{service.port}")
            self.assertEqual(PeerClient(peer, "123456").info()["device_id"], "test-ipv6")
        finally:
            service.stop()


class LockerNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = LockerConfig(
            "peer-test",
            "Peer",
            str(self.root / "remote"),
            hash_access_code("123456"),
            "123456",
            deny_patterns=["**/*.key", "private/**"],
            bind_address="127.0.0.1",
            port=0,
        )
        self.remote = Locker(self.config.locker_path, self.config.allow_patterns, self.config.deny_patterns)
        self.server = LockerHTTPServer(("127.0.0.1", 0), self.remote, self.config)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.peer = Peer("peer-test", "Peer", "127.0.0.1", self.server.server_port)
        self.client = PeerClient(self.peer, "123456", timeout=2)

    def stop_server(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)

    def test_file_round_trip_with_spaces_unicode_and_literal_percent(self):
        for name, data in (("hello world ü %2f.txt", b"content\x00" * 200_000), ("empty", b"")):
            with self.subTest(name=name):
                source = self.root / name
                source.write_bytes(data)
                self.client.upload(source, name)
                destination = self.root / "downloads" / name
                self.client.download(name, destination)
                self.assertEqual(destination.read_bytes(), data)
                self.assertEqual((self.remote.root / name).read_bytes(), data)
        self.assertEqual(
            {path.name for path in self.remote.root.glob(".fastfiles-*")},
            {".fastfiles-activity.json", ".fastfiles-activity.lock"},
        )
        receipts = ActivityStore(self.remote.root).recent()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["bytes"], 0)
        self.assertNotIn(".fastfiles-activity.json", [entry["name"] for entry in self.client.list()])

    def test_recursive_copy_preserves_empty_folders_and_respects_policy(self):
        local = Locker(self.root / "local", deny_patterns=["**/*.key"])
        (local.root / "project" / "empty").mkdir(parents=True)
        (local.root / "project" / "a.txt").write_text("shared")
        (local.root / "project" / "secret.key").write_text("private")
        progress = []
        result = copy_to_peer(
            local,
            self.client,
            "project",
            "",
            lambda done, total: progress.append((done, total)),
            threading.Event(),
        )
        self.assertEqual((result.files, result.directories, result.bytes), (1, 2, 6))
        self.assertTrue((self.remote.root / "project" / "empty").is_dir())
        self.assertFalse((self.remote.root / "project" / "secret.key").exists())
        self.assertEqual(progress[-1], (6, 6))
        receiving = Locker(self.root / "receiving")
        copy_from_peer(receiving, self.client, "project", True, "", lambda *_: None, threading.Event())
        self.assertEqual((receiving.root / "project" / "a.txt").read_text(), "shared")
        self.assertTrue((receiving.root / "project" / "empty").is_dir())

    def test_receive_policy_is_checked_before_writing(self):
        (self.remote.root / "file.txt").write_text("data")
        local = Locker(self.root / "local", [])
        with self.assertRaises(PermissionError):
            copy_from_peer(local, self.client, "file.txt", False, "", lambda *_: None, threading.Event())
        self.assertEqual(list(local.root.iterdir()), [])

    def test_wrong_code_and_read_only_peer(self):
        with self.assertRaisesRegex(OSError, "401"):
            PeerClient(self.peer, "654321").list()
        self.config.read_only = True
        source = self.root / "source"
        source.write_bytes(b"data")
        with self.assertRaisesRegex(OSError, "403"):
            self.client.upload(source, "source")
        self.assertFalse((self.remote.root / "source").exists())

    def test_upload_requires_explicit_overwrite(self):
        source = self.root / "source"
        source.write_text("first")
        self.client.upload(source, "target")
        source.write_text("second")
        with self.assertRaisesRegex(OSError, "409"):
            self.client.upload(source, "target")
        self.assertEqual((self.remote.root / "target").read_text(), "first")
        self.client.upload(source, "target", overwrite=True)
        self.assertEqual((self.remote.root / "target").read_text(), "second")

    def test_download_requires_explicit_overwrite(self):
        (self.remote.root / "source").write_text("new")
        destination = self.root / "destination"
        destination.write_text("old")
        with self.assertRaises(FileExistsError):
            self.client.download("source", destination)
        self.assertEqual(destination.read_text(), "old")
        self.client.download("source", destination, overwrite=True)
        self.assertEqual(destination.read_text(), "new")

    def test_interrupted_upload_does_not_replace_existing_file(self):
        (self.remote.root / "target").write_bytes(b"original")
        connection = http.client.HTTPConnection(self.peer.address, self.peer.port, timeout=2)
        self.addCleanup(connection.close)
        connection.putrequest("PUT", "/v1/upload?path=target&overwrite=1")
        connection.putheader("X-FastFiles-Code", "123456")
        connection.putheader("Content-Length", "9999")
        connection.endheaders(b"partial")
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        self.assertEqual((self.remote.root / "target").read_bytes(), b"original")

    def test_raw_requests_cannot_bypass_deny_or_traversal_checks(self):
        (self.remote.root / "private").mkdir()
        (self.remote.root / "private" / "data").write_text("hidden")
        for path in ("private/data", "../outside", "/tmp/absolute"):
            with self.subTest(path=path), self.assertRaises(OSError):
                self.client.download(path, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_public_availability_contains_no_root_or_access_code(self):
        public = PeerClient(self.peer, "").info()
        self.assertNotIn("locker_path", public)
        self.assertNotIn("access_code", public)
        self.assertEqual(probe_peer(self.peer), "Online")
        self.assertEqual(len(check_peers([self.peer, self.peer])), 1)
        self.stop_server()
        self.assertEqual(probe_peer(self.peer, timeout=0.1), "Offline")

    def test_cancelled_transfer_does_not_start(self):
        event = threading.Event()
        event.set()
        source = self.root / "source"
        source.write_text("data")
        with self.assertRaises(TransferCancelled):
            self.client.upload(source, "source", cancel=event)
        self.assertFalse((self.remote.root / "source").exists())

    def test_incomplete_download_keeps_previous_destination(self):
        class ShortResponse(io.BytesIO):
            status = 200
            headers = {"Content-Length": "100"}

        destination = self.root / "out"
        destination.write_bytes(b"original")
        with patch.object(self.client, "_open", return_value=ShortResponse(b"short")):
            with self.assertRaisesRegex(OSError, "Incomplete"):
                self.client.download("source", destination, overwrite=True)
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob(".fastfiles-*")))

    def test_malicious_remote_listing_is_rejected(self):
        for name, path in (
            ("../outside", "folder/../outside"),
            ("file", "unrelated/file"),
            ("/tmp/absolute", "/tmp/absolute"),
        ):
            listing = {"items": [{"name": name, "path": path, "is_dir": False, "size": 1}]}
            with self.subTest(name=name), patch.object(self.client, "_json", return_value=listing):
                with self.assertRaises(OSError):
                    self.client.list("folder")

    def test_malformed_optional_peer_capabilities_are_rejected(self):
        for key, value in (
            ("can_download", "false"), ("can_upload", 1), ("read_only", []),
            ("computer_name", None), ("download_patterns", "**"),
            ("upload_patterns", ["../outside/**"]), ("download_patterns", [None]),
        ):
            with self.subTest(key=key, value=value), patch.object(self.client, "_json", return_value={key: value}):
                with self.assertRaises(OSError):
                    self.client.info()
        with patch.object(self.client, "_json", return_value={"device_name": "Older peer"}):
            self.assertEqual(self.client.info(), {"device_name": "Older peer"})
        for key in ("can_download", "can_upload"):
            entry = {"name": "photo.jpg", "path": "photo.jpg", "is_dir": False, "size": 1, key: "yes"}
            with self.subTest(key=key), patch.object(self.client, "_json", return_value={"items": [entry]}):
                with self.assertRaises(OSError):
                    self.client.list()

    def test_auth_attempts_are_limited(self):
        wrong = PeerClient(self.peer, "654321")
        for _ in range(10):
            with self.assertRaisesRegex(OSError, "401"):
                wrong.list()
        with self.assertRaisesRegex(OSError, "429"):
            wrong.list()

    def test_invalid_content_length_is_rejected(self):
        for length in ("-1", "not-a-number", str(self.config.max_upload_bytes + 1)):
            connection = http.client.HTTPConnection(self.peer.address, self.peer.port, timeout=2)
            try:
                connection.request(
                    "PUT",
                    "/v1/upload?path=target",
                    headers={"X-FastFiles-Code": "123456", "Content-Length": length},
                )
                response = connection.getresponse()
                self.assertIn(response.status, (400, 413))
                response.read()
            finally:
                connection.close()
        self.assertFalse((self.remote.root / "target").exists())

    def grant(self, name="Laptop", download=None, upload=None):
        entry, token = new_computer_permission(name, download or [], upload or [])
        self.config.computer_permissions.append(entry)
        self.config.computer_access_enabled = True
        return PeerClient(self.peer, token, timeout=2)

    def test_computer_keys_cannot_be_bypassed_with_shared_code_or_device_name(self):
        permitted = self.grant(download=["**"], upload=["**"])
        self.assertEqual(permitted.info()["computer_name"], "Laptop")
        for value in ("123456", "Laptop", "peer-test"):
            client = PeerClient(self.peer, value)
            with self.subTest(value=value), self.assertRaisesRegex(OSError, "401"):
                client.list()
        # Public discovery is still possible, but exposes neither grants nor keys.
        info = PeerClient(self.peer, "").info()
        self.assertNotIn("download_patterns", info)
        self.assertNotIn("computer_permissions", info)
        self.assertNotIn("token_hash", json.dumps(info))
        self.config.computer_permissions.clear()
        for client in (permitted, self.client):
            with self.assertRaisesRegex(OSError, "401"):
                client.list()

    def test_manually_configured_computer_grants_disable_shared_code(self):
        allowed_client = self.grant(download=["**"])
        self.config.computer_access_enabled = False
        self.assertEqual(allowed_client.list(), [])
        with self.assertRaisesRegex(OSError, "401"):
            self.client.list()

    def test_download_and_upload_permissions_are_independent_and_filter_listing(self):
        (self.remote.root / "Photos").mkdir()
        (self.remote.root / "Photos" / "image.jpg").write_bytes(b"photo")
        (self.remote.root / "Inbox").mkdir()
        (self.remote.root / "Inbox" / "existing.jpg").write_bytes(b"someone else's photo")
        (self.remote.root / "Other").mkdir()
        (self.remote.root / "Other" / "file").write_text("hidden")
        client = self.grant(download=["Photos/**"], upload=["Inbox/**"])
        listing = {entry["name"]: entry for entry in client.list()}
        self.assertEqual(set(listing), {"Photos", "Inbox"})
        self.assertTrue(listing["Photos"]["can_download"])
        self.assertFalse(listing["Photos"]["can_upload"])
        self.assertFalse(listing["Inbox"]["can_download"])
        self.assertTrue(listing["Inbox"]["can_upload"])
        self.assertEqual(client.list("Inbox"), [])
        client.download("Photos/image.jpg", self.root / "photo")
        self.assertEqual((self.root / "photo").read_bytes(), b"photo")
        for path in ("Inbox/existing.jpg", "Inbox/missing.jpg", "Other/file", "Photos/../Inbox/existing.jpg"):
            with self.subTest(path=path), self.assertRaisesRegex(OSError, "403"):
                client.download(path, self.root / "blocked")
        with self.assertRaisesRegex(OSError, "403"):
            client.list("Other")
        source = self.root / "source"
        source.write_bytes(b"incoming photo")
        client.upload(source, "Inbox/new/photo.jpg")
        self.assertEqual((self.remote.root / "Inbox/new/photo.jpg").read_bytes(), b"incoming photo")
        for path in ("Photos/new.jpg", "Other/new.jpg", "new.jpg", "Inbox/../new.jpg"):
            with self.subTest(path=path), self.assertRaises(OSError):
                client.upload(source, path)
        receipt = ActivityStore(self.remote.root).recent()[0]
        self.assertEqual(receipt["source"], "Laptop")
        self.assertEqual(receipt["destination"], str(self.remote.root / "Inbox/new/photo.jpg"))
        self.assertEqual(receipt["files"], 1)
        self.assertEqual(receipt["bytes"], len(b"incoming photo"))
        self.assertNotIn(client.code, json.dumps(receipt))

    def test_nested_folder_grants_allow_navigation_without_sibling_files(self):
        (self.remote.root / "Shared/Photos").mkdir(parents=True)
        (self.remote.root / "Shared/Photos/a.jpg").write_bytes(b"photo")
        (self.remote.root / "Shared/private.txt").write_text("hidden")
        client = self.grant(download=["Shared/Photos/**"])
        self.assertEqual([item["name"] for item in client.list()], ["Shared"])
        self.assertEqual([item["name"] for item in client.list("Shared")], ["Photos"])
        self.assertEqual([item["name"] for item in client.list("Shared/Photos")], ["a.jpg"])
        with self.assertRaisesRegex(OSError, "403"):
            client.download("Shared/private.txt", self.root / "out")

    def test_file_pattern_grants_support_nested_browsing(self):
        (self.remote.root / "Photos/Trip").mkdir(parents=True)
        (self.remote.root / "Photos/Trip/a.jpg").write_bytes(b"photo")
        (self.remote.root / "Photos/Trip/a.txt").write_text("hidden")
        client = self.grant(download=["**/*.jpg"])
        self.assertEqual([item["name"] for item in client.list()], ["Photos"])
        self.assertEqual([item["name"] for item in client.list("Photos")], ["Trip"])
        self.assertEqual([item["name"] for item in client.list("Photos/Trip")], ["a.jpg"])

    def test_global_policy_and_read_only_bound_computer_grants(self):
        client = self.grant(download=["**"], upload=["**"])
        source = self.root / "source"
        source.write_text("data")
        for path in ("private/new.txt", "secret.key", ".fastfiles-activity.json", "../outside"):
            with self.subTest(path=path), self.assertRaises(OSError):
                client.upload(source, path)
        self.config.read_only = True
        self.assertFalse(client.info()["can_upload"])
        with self.assertRaisesRegex(OSError, "403"):
            client.mkdir("folder")
        with self.assertRaisesRegex(OSError, "403"):
            client.upload(source, "new.txt")
        self.assertEqual(ActivityStore(self.remote.root).recent(), [])

    def test_upload_may_not_create_ungranted_parents(self):
        source = self.root / "source"
        source.write_text("data")
        client = self.grant(upload=["Inbox/photo.jpg"])
        with self.assertRaisesRegex(OSError, "403"):
            client.upload(source, "Inbox/photo.jpg")
        self.assertFalse((self.remote.root / "Inbox").exists())
        (self.remote.root / "Inbox").mkdir()
        client.upload(source, "Inbox/photo.jpg")
        self.assertEqual((self.remote.root / "Inbox/photo.jpg").read_text(), "data")

    def test_global_parent_policy_is_not_bypassed_by_leaf_grant(self):
        self.remote.allow_patterns = ["Inbox/photo.jpg"]
        client = self.grant(upload=["**"])
        source = self.root / "source"
        source.write_text("data")
        with self.assertRaisesRegex(OSError, "403"):
            client.upload(source, "Inbox/photo.jpg")
        self.assertFalse((self.remote.root / "Inbox").exists())

    def test_runtime_revocation_uses_request_snapshot_and_rejects_next_request(self):
        client = self.grant(download=["**"])
        (self.remote.root / "photo.jpg").write_bytes(b"photo")
        captured = threading.Event()
        resume = threading.Event()
        original = LockerRequestHandler._authorized
        results = []

        def paused_authorization(handler):
            accepted = original(handler)
            captured.set()
            if not resume.wait(3):
                raise TimeoutError("Test did not resume the request")
            return accepted

        def download():
            try:
                client.download("photo.jpg", self.root / "downloaded.jpg")
                results.append("completed")
            except Exception as error:
                results.append(error)

        with patch.object(LockerRequestHandler, "_authorized", paused_authorization):
            worker = threading.Thread(target=download)
            worker.start()
            try:
                self.assertTrue(captured.wait(2))
                # In-place changes to the owner's old grant cannot mutate an
                # already authenticated request's detached permissions.
                self.config.computer_permissions[0]["download_patterns"].clear()
                self.config.save_computer_permissions([], self.root / "config.json")
            finally:
                resume.set()
                worker.join(3)
        self.assertEqual(results, ["completed"])
        self.assertEqual((self.root / "downloaded.jpg").read_bytes(), b"photo")
        for rejected in (client, self.client):
            with self.assertRaisesRegex(OSError, "401"):
                rejected.list()

    def test_failed_permission_save_preserves_running_and_saved_access(self):
        client = self.grant(download=["**"])
        path = self.root / "config.json"
        self.config.save(path)
        before = path.read_bytes()
        with patch("fastfiles.locker.write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.config.save_computer_permissions([], path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(client.info()["computer_name"], "Laptop")
        with self.assertRaisesRegex(OSError, "401"):
            self.client.list()


if __name__ == "__main__":
    unittest.main()
