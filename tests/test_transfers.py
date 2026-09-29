import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastfiles.access import new_computer_permission
from fastfiles.locker import (
    Locker,
    LockerConfig,
    LockerHTTPServer,
    Peer,
    PeerClient,
    TransferCancelled,
    hash_access_code,
)
from fastfiles.transfers import copy_from_peer, copy_many_from_peer, copy_many_to_peer, upload_paths_to_peer


class SelectionTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = Locker(self.root / "local")
        self.remote = Locker(self.root / "remote", deny_patterns=["**/*.key"])
        self.config = LockerConfig(
            "computer", "Other computer", str(self.remote.root), hash_access_code("123456"), "123456",
            deny_patterns=["**/*.key"], bind_address="127.0.0.1", port=0,
        )
        self.server = LockerHTTPServer(("127.0.0.1", 0), self.remote, self.config)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client = PeerClient(Peer("computer", "Other computer", "127.0.0.1", self.server.server_port),
                                 "123456", timeout=2)
        self.cancel = threading.Event()
        self.progress = []

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def track(self, done, total):
        self.progress.append((done, total))

    def test_multiselection_round_trip_preserves_photos_and_empty_folders(self):
        photo = b"photo data\0" * 200_000
        (self.local.root / "photo ü.jpg").write_bytes(photo)
        (self.local.root / "album" / "empty").mkdir(parents=True)
        (self.local.root / "album" / "second.png").write_bytes(b"second photo")
        (self.remote.root / "inbox").mkdir()
        result = copy_many_to_peer(self.local, self.client, ["photo ü.jpg", "album"], "inbox",
                                   self.track, self.cancel)
        expected = len(photo) + len(b"second photo")
        self.assertEqual((result.files, result.directories, result.bytes), (2, 2, expected))
        self.assertTrue(result.destination.endswith("/inbox"))
        self.assertEqual(self.progress[0], (0, expected))
        self.assertEqual(self.progress[-1], (expected, expected))
        self.assertEqual(self.progress, sorted(self.progress))
        self.assertTrue(all(total == expected for _, total in self.progress))
        received = Locker(self.root / "received")
        result = copy_many_from_peer(received, self.client,
                                     [("inbox/photo ü.jpg", False), ("inbox/album", True)], "",
                                     self.track, self.cancel)
        self.assertEqual(result.bytes, expected)
        self.assertEqual(result.destination, str(received.root))
        self.assertEqual((received.root / "photo ü.jpg").read_bytes(), photo)
        self.assertEqual((received.root / "album" / "second.png").read_bytes(), b"second photo")
        self.assertTrue((received.root / "album" / "empty").is_dir())
        self.assertEqual((self.local.root / "photo ü.jpg").read_bytes(), photo)

    def test_external_drop_sends_directly_without_staging_or_moving(self):
        external = self.root / "external"
        (external / "album" / "empty").mkdir(parents=True)
        (external / "album" / "photo.jpg").write_bytes(b"photo")
        video = external / "video.mp4"
        video.write_bytes(b"video")
        result = upload_paths_to_peer(self.client, [str(external / "album"), str(video)], "",
                                      self.track, self.cancel)
        self.assertEqual((result.files, result.directories, result.bytes), (2, 2, 10))
        self.assertEqual((self.remote.root / "album" / "photo.jpg").read_bytes(), b"photo")
        self.assertTrue((self.remote.root / "album" / "empty").is_dir())
        self.assertEqual((self.remote.root / "video.mp4").read_bytes(), b"video")
        self.assertEqual(video.read_bytes(), b"video")
        self.assertEqual(list(self.local.root.iterdir()), [])
        self.assertEqual(self.progress[-1], (10, 10))

    def test_external_drop_cannot_overwrite_without_explicit_choice(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"new photo")
        target = self.remote.root / source.name
        target.write_bytes(b"old photo")
        with self.assertRaises(OSError):
            upload_paths_to_peer(self.client, [str(source)], "", self.track, self.cancel)
        self.assertEqual(target.read_bytes(), b"old photo")
        upload_paths_to_peer(self.client, [str(source)], "", self.track, self.cancel, overwrite=True)
        self.assertEqual(target.read_bytes(), b"new photo")

    def test_upload_grants_are_checked_for_entire_selection_before_any_write(self):
        entry, token = new_computer_permission("Photo sender", [], ["first.jpg"])
        self.config.computer_permissions = [entry]
        self.config.computer_access_enabled = True
        client = PeerClient(self.client.peer, token, timeout=2)
        for name in ("first.jpg", "second.png"):
            (self.local.root / name).write_bytes(b"photo")
        for external in (False, True):
            with self.subTest(external=external), self.assertRaises(PermissionError):
                if external:
                    upload_paths_to_peer(client, [str(self.local.root / name) for name in ("first.jpg", "second.png")],
                                         "", self.track, self.cancel)
                else:
                    copy_many_to_peer(self.local, client, ["first.jpg", "second.png"], "",
                                      self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_denied_child_upload_is_checked_before_creating_parent_folder(self):
        entry, token = new_computer_permission("Folder only", [], ["album"])
        self.config.computer_permissions = [entry]
        self.config.computer_access_enabled = True
        client = PeerClient(self.client.peer, token, timeout=2)
        (self.local.root / "album").mkdir()
        (self.local.root / "album" / "photo.jpg").write_bytes(b"photo")
        with self.assertRaises(PermissionError):
            copy_many_to_peer(self.local, client, ["album"], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_upload_preflight_supports_older_peers_without_pattern_capabilities(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"photo")
        with patch.object(self.client, "info", return_value={"read_only": False}):
            result = upload_paths_to_peer(self.client, [str(source)], "", self.track, self.cancel)
        self.assertEqual(result.files, 1)
        self.assertEqual((self.remote.root / source.name).read_bytes(), b"photo")

    def test_receive_conflicts_are_checked_for_entire_selection_before_writing(self):
        (self.remote.root / "first.jpg").write_bytes(b"first")
        (self.remote.root / "second.jpg").write_bytes(b"second")
        (self.local.root / "second.jpg").write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            copy_many_from_peer(self.local, self.client, [("first.jpg", False), ("second.jpg", False)],
                                "", self.track, self.cancel)
        self.assertFalse((self.local.root / "first.jpg").exists())
        self.assertEqual((self.local.root / "second.jpg").read_bytes(), b"original")

    def test_receive_does_not_copy_filtered_files(self):
        (self.remote.root / "album").mkdir()
        (self.remote.root / "album" / "photo.jpg").write_bytes(b"photo")
        (self.remote.root / "album" / "secret.key").write_bytes(b"secret")
        result = copy_many_from_peer(self.local, self.client, [("album", True)], "", self.track, self.cancel)
        self.assertEqual(result.files, 1)
        self.assertFalse((self.local.root / "album" / "secret.key").exists())

    def test_upload_only_folder_cannot_be_received_as_an_empty_success(self):
        entry, token = new_computer_permission("Uploader", [], ["Inbox/**"])
        self.config.computer_permissions = [entry]
        self.config.computer_access_enabled = True
        client = PeerClient(self.client.peer, token, timeout=2)
        (self.remote.root / "Inbox").mkdir()
        (self.remote.root / "Inbox" / "private.jpg").write_bytes(b"private")
        for single in (False, True):
            with self.subTest(single=single), self.assertRaises(PermissionError):
                if single:
                    copy_from_peer(self.local, client, "Inbox", True, "", self.track, self.cancel)
                else:
                    copy_many_from_peer(self.local, client, [("Inbox", True)], "", self.track, self.cancel)
        self.assertEqual(list(self.local.root.iterdir()), [])

    def test_navigation_only_folder_requires_selecting_downloadable_child(self):
        entry, token = new_computer_permission("Viewer", ["Photos/Public/**"], [])
        self.config.computer_permissions = [entry]
        self.config.computer_access_enabled = True
        client = PeerClient(self.client.peer, token, timeout=2)
        (self.remote.root / "Photos" / "Public").mkdir(parents=True)
        (self.remote.root / "Photos" / "Public" / "photo.jpg").write_bytes(b"photo")
        with self.assertRaises(PermissionError):
            copy_many_from_peer(self.local, client, [("Photos", True)], "", self.track, self.cancel)
        self.assertEqual(list(self.local.root.iterdir()), [])
        result = copy_many_from_peer(self.local, client, [("Photos/Public", True)], "", self.track, self.cancel)
        self.assertEqual(result.files, 1)
        self.assertEqual((self.local.root / "Public" / "photo.jpg").read_bytes(), b"photo")

    def test_denied_folder_in_multiselection_is_checked_before_receiving_any_file(self):
        entry, token = new_computer_permission("Mixed access", ["photo.jpg"], ["Inbox/**"])
        self.config.computer_permissions = [entry]
        self.config.computer_access_enabled = True
        client = PeerClient(self.client.peer, token, timeout=2)
        (self.remote.root / "photo.jpg").write_bytes(b"photo")
        (self.remote.root / "Inbox").mkdir()
        with self.assertRaises(PermissionError):
            copy_many_from_peer(self.local, client, [("photo.jpg", False), ("Inbox", True)], "",
                                self.track, self.cancel)
        self.assertEqual(list(self.local.root.iterdir()), [])

    def test_receive_can_create_destination_and_counts_changed_file_bytes(self):
        source = self.remote.root / "photo.jpg"
        source.write_bytes(b"old")
        original_download = self.client.download

        def download_updated_file(*args, **kwargs):
            source.write_bytes(b"updated photo")
            return original_download(*args, **kwargs)

        with patch.object(self.client, "download", side_effect=download_updated_file):
            result = copy_many_from_peer(self.local, self.client, [("photo.jpg", False)], "new/folder",
                                         self.track, self.cancel)
        self.assertEqual((self.local.root / "new" / "folder" / "photo.jpg").read_bytes(), b"updated photo")
        self.assertEqual(result.bytes, len(b"updated photo"))
        self.assertEqual(self.progress[-1], (result.bytes, result.bytes))
        self.assertTrue(all(done <= total for done, total in self.progress))

    def test_duplicate_basenames_are_rejected_before_network_writes(self):
        for folder in ("one", "two"):
            (self.local.root / folder).mkdir()
            (self.local.root / folder / "same.jpg").write_bytes(folder.encode())
        with self.assertRaises(ValueError):
            copy_many_to_peer(self.local, self.client, ["one/same.jpg", "two/same.jpg"], "",
                              self.track, self.cancel)
        with self.assertRaises(ValueError):
            upload_paths_to_peer(self.client, [str(self.local.root / "one" / "same.jpg"),
                                              str(self.local.root / "two" / "same.jpg")], "",
                                 self.track, self.cancel)
        with self.assertRaises(ValueError):
            copy_many_from_peer(self.local, self.client, [("one/same.jpg", False), ("two/same.jpg", False)],
                                "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_drop_with_symlink_is_rejected_before_any_upload(self):
        source = self.root / "external"
        source.mkdir()
        (source / "a-photo.jpg").write_bytes(b"photo")
        link = source / "z-link"
        try:
            link.symlink_to(source / "a-photo.jpg")
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows symlinks require Developer Mode or administrator privileges")
            raise
        with self.assertRaisesRegex(ValueError, "Links and junctions"):
            upload_paths_to_peer(self.client, [str(source)], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_drop_through_linked_parent_is_rejected(self):
        source = self.root / "external"
        source.mkdir()
        (source / "photo.jpg").write_bytes(b"photo")
        link = self.root / "linked"
        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows symlinks require Developer Mode or administrator privileges")
            raise
        with self.assertRaisesRegex(ValueError, "Links and junctions"):
            upload_paths_to_peer(self.client, [str(link / "photo.jpg")], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes unavailable")
    def test_special_files_are_rejected_before_any_upload(self):
        source = self.root / "external"
        source.mkdir()
        (source / "a-photo.jpg").write_bytes(b"photo")
        os.mkfifo(source / "z-pipe")
        with self.assertRaisesRegex(ValueError, "Only regular files"):
            upload_paths_to_peer(self.client, [str(source)], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_cancelled_multiselect_does_not_create_files(self):
        (self.local.root / "photo.jpg").write_bytes(b"photo")
        self.cancel.set()
        with self.assertRaises(TransferCancelled):
            copy_many_to_peer(self.local, self.client, ["photo.jpg"], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])

    def test_external_names_and_destination_paths_are_validated_before_upload(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"photo")
        with self.assertRaises(PermissionError):
            upload_paths_to_peer(self.client, [str(source)], "../outside", self.track, self.cancel)
        hidden = self.root / ".fastfiles-working"
        hidden.write_bytes(b"private")
        with self.assertRaises(PermissionError):
            upload_paths_to_peer(self.client, [str(source), str(hidden)], "", self.track, self.cancel)
        self.assertEqual(list(self.remote.root.iterdir()), [])
