import json
import tempfile
import unittest
from pathlib import Path

from app.models import CredentialsModel, ProjectModel
from app.storage import ProjectStorage
from app.utils import normalize_relative_agent_path, obfuscate_secret, reveal_secret


class UtilsTestCase(unittest.TestCase):
    def test_secret_obfuscation_roundtrip(self) -> None:
        original = "SuperSecret123!"
        obfuscated = obfuscate_secret(original, salt="test-salt")
        self.assertNotEqual(original, obfuscated)
        revealed = reveal_secret(obfuscated, salt="test-salt")
        self.assertEqual(original, revealed)

    def test_normalize_relative_path_rejects_absolute(self) -> None:
        with self.assertRaises(ValueError):
            normalize_relative_agent_path("/etc/1c")

    def test_normalize_relative_path_passes_relative(self) -> None:
        normalized = normalize_relative_agent_path("../path/to/config")
        self.assertEqual("../path/to/config", normalized)


class StorageTestCase(unittest.TestCase):
    def test_storage_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "projects.json"
            storage = ProjectStorage(path)

            project = ProjectModel(
                name="Demo",
                description="Test project",
                credentials=CredentialsModel(
                    host="127.0.0.1",
                    port=2222,
                    username="user",
                    password="secret",
                    keepalive_interval=10,
                    timeout=15,
                ),
                config_dir="../config",
                extensions_dir="../extensions",
                externals_dir="../external",
                externals_xml_dir="../external-xml",
                extensions=["ExtA", "ExtB"],
                external_objects=["Report.epf"],
            )

            saved = storage.upsert(project)
            stored = json.loads(path.read_text(encoding="utf-8"))
            credentials = stored["projects"][0]["credentials"]
            self.assertNotEqual("secret", credentials.get("password"))
            self.assertTrue(stored["projects"][0]["uid"])

            loaded = storage.get_project(saved.uid)
            assert loaded is not None
            self.assertEqual("secret", loaded.credentials.password)
            self.assertEqual("../config", loaded.config_dir)

            # Rename the project and ensure no duplicates remain
            renamed = saved.copy(update={"name": "Renamed"})
            storage.upsert(renamed, previous_uid=saved.uid)
            listing = list(storage.list_projects())
            self.assertEqual(1, len(listing))
            self.assertEqual("Renamed", listing[0].name)


if __name__ == "__main__":
    unittest.main()
