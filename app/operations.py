"""
Shared operations builders for the 1C agent commands.
"""

from __future__ import annotations

import posixpath
from typing import List

from .models import ProjectModel, RunOptionsModel


def build_dump_config_sequence(project: ProjectModel) -> List[str]:
    command = build_config_command(
        "dump-config-to-files",
        project.config_dir,
        project.options,
        allow_dump_flags=True,
        allow_server_flags=True,
    )
    return ["common connect-ib", command, "common disconnect-ib"]


def build_load_config_sequence(project: ProjectModel) -> List[str]:
    command = build_config_command(
        "load-config-from-files",
        project.config_dir,
        project.options,
        load=True,
        allow_load_flags=True,
    )
    return ["common connect-ib", command, "common disconnect-ib"]


def build_dump_extensions_sequence(project: ProjectModel) -> List[str]:
    commands = ["common connect-ib"]
    if project.extensions:
        for name in project.extensions:
            per_extension_command = build_config_command(
                "dump-config-to-files",
                join_remote(project.extensions_dir, name),
                project.options,
            )
            commands.append(f'{per_extension_command} --extension="{name}"')
    else:
        base = build_config_command(
            "dump-config-to-files",
            project.extensions_dir,
            project.options,
        )
        commands.append(f"{base} --all-extensions")
    commands.append("common disconnect-ib")
    return commands


def build_load_extensions_sequence(project: ProjectModel) -> List[str]:
    commands = ["common connect-ib"]
    if project.extensions:
        for name in project.extensions:
            per_extension_command = build_config_command(
                "load-config-from-files",
                join_remote(project.extensions_dir, name),
                project.options,
                load=True,
            )
            commands.append(f'{per_extension_command} --extension="{name}"')
    else:
        base = build_config_command(
            "load-config-from-files",
            project.extensions_dir,
            project.options,
            load=True,
        )
        commands.append(f"{base} --all-extensions")
    commands.append("common disconnect-ib")
    return commands


def build_dump_externals_sequence(project: ProjectModel) -> List[str]:
    commands = ["common connect-ib"]
    for file_name in project.external_objects:
        ext_path = join_remote(project.externals_dir, file_name)
        stem = posixpath.splitext(file_name)[0]
        xml_dir = join_remote(project.externals_xml_dir, stem)
        xml_path = posixpath.join(xml_dir, f"{stem}.xml")
        commands.append(
            f'config dump-external-data-processor-or-report-to-files --file="{xml_path}" --ext-file="{ext_path}"'
        )
    commands.append("common disconnect-ib")
    return commands


def build_load_externals_sequence(project: ProjectModel) -> List[str]:
    commands = ["common connect-ib"]
    for file_name in project.external_objects:
        ext_path = join_remote(project.externals_dir, file_name)
        stem = posixpath.splitext(file_name)[0]
        xml_dir = join_remote(project.externals_xml_dir, stem)
        xml_path = posixpath.join(xml_dir, f"{stem}.xml")
        commands.append(
            f'config load-external-data-processor-or-report-from-files --file="{xml_path}" --ext-file="{ext_path}"'
        )
    commands.append("common disconnect-ib")
    return commands


def build_config_command(
    action: str,
    directory: str,
    options: RunOptionsModel,
    load: bool = False,
    allow_dump_flags: bool = False,
    allow_load_flags: bool = False,
    allow_server_flags: bool = False,
) -> str:
    command = f'config {action} --dir="{directory}"'
    if not load and allow_dump_flags:
        if options.update:
            command += " --update"
        if options.force:
            command += " --force"
        if options.ignore_unresolved_refs:
            command += " --ignore-unresolved-refs"
    if load and allow_load_flags:
        if options.no_check:
            command += " --no-check"
        if options.update_config_dump_info:
            command += " --update-config-dump-info"
    if allow_server_flags and options.use_server:
        command += " --server"
        if options.threads:
            command += f" --threads={options.threads}"
    return command


def join_remote(base: str, name: str) -> str:
    return str(posixpath.join(base.rstrip("/"), name))
