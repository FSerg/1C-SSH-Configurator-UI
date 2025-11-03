import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

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
            # Defaults for new fields
            self.assertIs(False, getattr(loaded, "keep_db_connection", False))
            self.assertEqual(3600, getattr(loaded, "ssh_idle_timeout_seconds", 3600))

            # Rename the project and ensure no duplicates remain
            renamed = saved.copy(update={"name": "Renamed"})
            storage.upsert(renamed, previous_uid=saved.uid)
            listing = list(storage.list_projects())
            self.assertEqual(1, len(listing))
            self.assertEqual("Renamed", listing[0].name)

    def test_copy_project_without_uid(self) -> None:
        """Тест создания копии проекта без uid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "projects.json"
            storage = ProjectStorage(path)

            # Создаём исходный проект
            original = ProjectModel(
                name="Original Project",
                description="Original description",
                credentials=CredentialsModel(
                    host="192.168.1.100",
                    port=22,
                    username="admin",
                    password="original_password",
                    keepalive_interval=30,
                    timeout=60,
                ),
                config_dir="../config",
                extensions_dir="../extensions",
                externals_dir="../external",
                externals_xml_dir="../external-xml",
                extensions=["Ext1", "Ext2"],
                external_objects=["Report.epf", "Processor.erf"],
            )

            saved = storage.upsert(original)
            original_uid = saved.uid

            # Создаём копию проекта (эмулируем поведение _copy_project_to_form)
            # Используем метод copy() модели и обновляем имя
            copied_project = original.copy(
                update={
                    "name": f"{original.name} (копия)",
                }
            )
            # Убираем uid, чтобы создать новый проект
            copied_project.uid = uuid4().hex
            copied_saved = storage.upsert(copied_project)

            # Проверяем, что оба проекта существуют
            listing = list(storage.list_projects())
            self.assertEqual(2, len(listing))

            # Проверяем, что у копии другой uid
            self.assertNotEqual(original_uid, copied_saved.uid)

            # Проверяем, что имя копии содержит суффикс " (копия)"
            self.assertEqual("Original Project (копия)", copied_saved.name)

            # Проверяем, что все данные скопированы корректно
            self.assertEqual(saved.description, copied_saved.description)
            self.assertEqual(saved.credentials.host, copied_saved.credentials.host)
            self.assertEqual(saved.credentials.port, copied_saved.credentials.port)
            self.assertEqual(saved.credentials.username, copied_saved.credentials.username)
            self.assertEqual(saved.credentials.password, copied_saved.credentials.password)
            self.assertEqual(saved.config_dir, copied_saved.config_dir)
            self.assertEqual(saved.extensions_dir, copied_saved.extensions_dir)
            self.assertEqual(saved.externals_dir, copied_saved.externals_dir)
            self.assertEqual(saved.externals_xml_dir, copied_saved.externals_xml_dir)
            self.assertEqual(saved.extensions, copied_saved.extensions)
            self.assertEqual(saved.external_objects, copied_saved.external_objects)
            self.assertEqual(saved.options.use_server, copied_saved.options.use_server)
            self.assertEqual(saved.options.command_timeout_seconds, copied_saved.options.command_timeout_seconds)


if __name__ == "__main__":
    unittest.main()
