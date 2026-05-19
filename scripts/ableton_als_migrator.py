#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import gzip
import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRACK_TAGS = {"AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack"}


def strip_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def get_attr(node: ET.Element | None, key: str = "Value", default: str = "") -> str:
    if node is None:
        return default
    return node.attrib.get(key, default)


def find_value(node: ET.Element, path: str, default: str = "") -> str:
    return get_attr(node.find(path), "Value", default)


def set_value(node: ET.Element, value: str) -> None:
    node.attrib["Value"] = value


def find_track_name(track: ET.Element) -> str:
    return (
        find_value(track, "./Name/UserName")
        or find_value(track, "./Name/EffectiveName")
        or find_value(track, "./Name/Annotation")
    )


def plugin_browser_path(plugin: ET.Element) -> str:
    return find_value(plugin, "./SourceContext/Value/BranchSourceContext/BrowserContentPath")


def plugin_branch_id(plugin: ET.Element) -> str:
    return find_value(plugin, "./SourceContext/Value/BranchSourceContext/BranchDeviceId")


def hex_text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return "".join(node.text.split())


def indent_xml(node: ET.Element, level: int = 0) -> None:
    indent = "\n" + level * "\t"
    if len(node):
        if not node.text or not node.text.strip():
            node.text = indent + "\t"
        for child in node:
            indent_xml(child, level + 1)
        if not node[-1].tail or not node[-1].tail.strip():
            node[-1].tail = indent
    if level and (not node.tail or not node.tail.strip()):
        node.tail = "\n" + (level - 1) * "\t"


def make_elem(tag: str, value: str | None = None, **attrs: str) -> ET.Element:
    node = ET.Element(tag)
    for key, attr_value in attrs.items():
        node.attrib[key] = attr_value
    if value is not None:
        node.attrib["Value"] = value
    return node


def append_value(parent: ET.Element, tag: str, value: str) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.attrib["Value"] = value
    return node


def build_uid(parent: ET.Element, fields: tuple[int, int, int, int]) -> ET.Element:
    uid = ET.SubElement(parent, "Uid")
    for idx, field_value in enumerate(fields):
        append_value(uid, f"Fields.{idx}", str(field_value))
    return uid


@dataclass(frozen=True)
class Vst3Spec:
    plugin_name: str
    browser_path: str
    branch_device_id: str
    uid_fields: tuple[int, int, int, int]
    processor_prefix_hex: str
    processor_suffix_hex: str
    parameter_id_map: dict[str, str]
    overwrite_protection_number: str


VALHALLA_PLATE_SPEC = Vst3Spec(
    plugin_name="ValhallaPlate",
    browser_path="query:Plugins#VST3:Valhalla%20DSP:ValhallaPlate",
    branch_device_id="device:vst3:audiofx:56535470-6c61-7476-616c-68616c6c6170",
    uid_fields=(1448301680, 1818326134, 1634494561, 1819042160),
    processor_prefix_hex=(
        "5673745700000008000000010000000043636E4B0000026A4642436800000002706C617400010608"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000001D2"
    ),
    processor_suffix_hex=(
        "0000000000000000004A554345507269766174654461746100010142797061737300010103001D"
        "000000000000004A5543455072697661746544617461"
    ),
    parameter_id_map={
        "Mix": "48",
        "PreDelay": "49",
        "Decay": "50",
        "Size": "51",
        "Width": "52",
        "ModRate": "53",
        "ModDepth": "54",
        "LowEQFreq": "55",
        "LowEQGain": "56",
        "HighEQFreq": "57",
        "HighEQGain": "1567",
        "Type": "1568",
    },
    overwrite_protection_number="3073",
)


ENDLESS_SMILE_SPEC = Vst3Spec(
    plugin_name="Endless Smile",
    browser_path="query:Plugins#VST3:Dada%20Life:Endless%20Smile",
    branch_device_id="device:vst3:audiofx:56535445-4e44-5365-6e64-6c6573732073",
    uid_fields=(1448301637, 1313100645, 1852075109, 1936924787),
    processor_prefix_hex=(
        "5673745700000008000000010000000043636E4B000000DE4642436800000002454E445300000001"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000046"
    ),
    processor_suffix_hex="",
    parameter_id_map={},
    overwrite_protection_number="3075",
)


MIGRATION_SPECS: dict[str, Vst3Spec] = {
    "query:Plugins#VST:Local:ValhallaPlate": VALHALLA_PLATE_SPEC,
    "query:Plugins#VST:Local:Endless%20Smile": ENDLESS_SMILE_SPEC,
}


REMOVE_BROWSER_MATCHES = ("Gullfoss",)


def build_vst3_plugin_desc(
    spec: Vst3Spec,
    original_plugin: ET.Element,
    raw_vst2_buffer: str,
) -> ET.Element:
    original_info = original_plugin.find("./PluginDesc/VstPluginInfo")
    original_preset = original_plugin.find("./PluginDesc/VstPluginInfo/Preset/VstPreset")
    info_source = original_info if original_info is not None else original_plugin
    preset_source = original_preset if original_preset is not None else original_plugin

    plugin_desc = ET.Element("PluginDesc")
    info = ET.SubElement(plugin_desc, "Vst3PluginInfo", {"Id": "0"})

    append_value(info, "WinPosX", find_value(info_source, "./WinPosX", "0"))
    append_value(info, "WinPosY", find_value(info_source, "./WinPosY", "0"))
    append_value(info, "NumAudioInputs", find_value(info_source, "./NumAudioInputs", "1"))
    append_value(info, "NumAudioOutputs", find_value(info_source, "./NumAudioOutputs", "1"))
    append_value(info, "IsPlaceholderDevice", "false")

    preset_wrapper = ET.SubElement(info, "Preset")
    preset_id = get_attr(original_preset, "Id", "0")
    preset = ET.SubElement(preset_wrapper, "Vst3Preset", {"Id": preset_id})
    append_value(
        preset,
        "OverwriteProtectionNumber",
        spec.overwrite_protection_number,
    )
    append_value(preset, "MpeEnabled", find_value(preset_source, "./MpeEnabled", "0"))
    mpe_settings = ET.SubElement(preset, "MpeSettings")
    append_value(mpe_settings, "ZoneType", find_value(preset_source, "./MpeSettings/ZoneType", "0"))
    append_value(
        mpe_settings,
        "FirstNoteChannel",
        find_value(preset_source, "./MpeSettings/FirstNoteChannel", "1"),
    )
    append_value(
        mpe_settings,
        "LastNoteChannel",
        find_value(preset_source, "./MpeSettings/LastNoteChannel", "15"),
    )
    ET.SubElement(preset, "ParameterSettings")
    append_value(preset, "IsOn", find_value(preset_source, "./IsOn", "true"))
    append_value(preset, "PowerMacroControlIndex", "-1")
    power_range = ET.SubElement(preset, "PowerMacroMappingRange")
    append_value(power_range, "Min", "64")
    append_value(power_range, "Max", "127")
    append_value(preset, "IsFolded", find_value(preset_source, "./IsFolded", "false"))
    append_value(preset, "StoredAllParameters", "true")
    append_value(preset, "DeviceLomId", "0")
    append_value(preset, "DeviceViewLomId", "0")
    append_value(preset, "IsOnLomId", "0")
    append_value(preset, "ParametersListWrapperLomId", "0")
    build_uid(preset, spec.uid_fields)
    append_value(preset, "DeviceType", "2")

    processor_state = ET.SubElement(preset, "ProcessorState")
    processor_state.text = spec.processor_prefix_hex + raw_vst2_buffer + spec.processor_suffix_hex
    ET.SubElement(preset, "ControllerState")
    append_value(preset, "Name", "")
    ET.SubElement(preset, "PresetRef")

    append_value(info, "Name", spec.plugin_name)
    build_uid(info, spec.uid_fields)
    append_value(info, "DeviceType", "2")

    return plugin_desc


def replace_plugin_desc(plugin: ET.Element, new_desc: ET.Element) -> None:
    old_desc = plugin.find("./PluginDesc")
    insert_index = len(plugin)
    if old_desc is not None:
        insert_index = list(plugin).index(old_desc)
        plugin.remove(old_desc)
    plugin.insert(insert_index, new_desc)


def remap_parameter_ids(plugin: ET.Element, spec: Vst3Spec) -> int:
    updated = 0
    if not spec.parameter_id_map:
        return updated
    for parameter in plugin.findall("./ParameterList/PluginFloatParameter"):
        name = find_value(parameter, "./ParameterName")
        if not name:
            continue
        new_id = spec.parameter_id_map.get(name)
        if not new_id:
            continue
        param_id_node = parameter.find("./ParameterId")
        if param_id_node is None:
            continue
        if get_attr(param_id_node) != new_id:
            set_value(param_id_node, new_id)
            updated += 1
    return updated


def collect_target_ids(plugin: ET.Element) -> set[str]:
    target_ids: set[str] = set()
    for parameter in plugin.findall("./ParameterList/PluginFloatParameter"):
        automation_id = find_value(parameter, "./ParameterValue/AutomationTarget", "")
        modulation_id = find_value(parameter, "./ParameterValue/ModulationTarget", "")
        if automation_id:
            target_ids.add(automation_id)
        if modulation_id:
            target_ids.add(modulation_id)
    return target_ids


def remove_automation_refs(track: ET.Element, target_ids: set[str]) -> int:
    removed = 0
    if not target_ids:
        return removed
    for parent in track.iter():
        for child in list(parent):
            if strip_tag(child.tag) != "AutomationEnvelope":
                continue
            target = find_value(child, "./EnvelopeTarget/PointeeId")
            if target and target in target_ids:
                parent.remove(child)
                removed += 1
    return removed


def plugin_summary(plugin: ET.Element) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "browser_path": plugin_browser_path(plugin),
        "branch_device_id": plugin_branch_id(plugin),
        "parameter_values": [],
        "ascii_runs": [],
        "xml_attributes": None,
    }
    for parameter in plugin.findall("./ParameterList/PluginFloatParameter"):
        param_name = find_value(parameter, "./ParameterName")
        manual = find_value(parameter, "./ParameterValue/Manual")
        param_id = find_value(parameter, "./ParameterId")
        if param_name or manual:
            summary["parameter_values"].append(
                {
                    "name": param_name,
                    "parameter_id": param_id,
                    "manual": manual,
                }
            )

    raw_hex = (
        hex_text(plugin.find("./PluginDesc/VstPluginInfo/Preset/VstPreset/Buffer"))
        or hex_text(plugin.find("./PluginDesc/Vst3PluginInfo/Preset/Vst3Preset/ProcessorState"))
    )
    if raw_hex:
        raw = bytes.fromhex(raw_hex)
        ascii_runs = [
            chunk.decode("latin1", "ignore")
            for chunk in re.findall(rb"[ -~]{4,}", raw)
        ]
        summary["ascii_runs"] = ascii_runs[:60]

        xml_match = re.search(rb"(<\?xml[^>]*>.*?>)", raw)
        if xml_match:
            pass
        full_xml = re.search(rb"(<\?xml[^>]*>.*?(?:/>|</[A-Za-z0-9_:-]+>))", raw)
        if full_xml:
            try:
                xml_root = ET.fromstring(full_xml.group(1).decode("utf-8", "replace"))
                summary["xml_attributes"] = {
                    "tag": strip_tag(xml_root.tag),
                    "attributes": dict(xml_root.attrib),
                }
            except ET.ParseError:
                summary["xml_attributes"] = None
    return summary


def extract_xml_attributes_from_hex(raw_hex: str) -> dict[str, str] | None:
    if not raw_hex:
        return None
    raw = bytes.fromhex(raw_hex)
    full_xml = re.search(rb"(<\?xml[^>]*>.*?(?:/>|</[A-Za-z0-9_:-]+>))", raw)
    if not full_xml:
        return None
    try:
        xml_root = ET.fromstring(full_xml.group(1).decode("utf-8", "replace"))
    except ET.ParseError:
        return None
    return dict(xml_root.attrib)


def sync_manual_values_from_attributes(plugin: ET.Element, attributes: dict[str, str] | None) -> int:
    if not attributes:
        return 0
    updated = 0
    for parameter in plugin.findall("./ParameterList/PluginFloatParameter"):
        name = find_value(parameter, "./ParameterName")
        if not name or name not in attributes:
            continue
        manual_node = parameter.find("./ParameterValue/Manual")
        if manual_node is None:
            continue
        new_value = attributes[name]
        if get_attr(manual_node) != new_value:
            set_value(manual_node, new_value)
            updated += 1
    return updated


def validate_hex_nodes(root: ET.Element) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for node in root.iter():
        tag = strip_tag(node.tag)
        if tag not in {"Buffer", "ProcessorState", "ControllerState"}:
            continue
        hex_value = hex_text(node)
        if not hex_value:
            continue
        if len(hex_value) % 2:
            issues.append(
                {
                    "tag": tag,
                    "reason": "odd_length",
                    "length": str(len(hex_value)),
                    "preview": hex_value[:64],
                }
            )
            continue
        try:
            bytes.fromhex(hex_value)
        except ValueError as exc:
            issues.append(
                {
                    "tag": tag,
                    "reason": str(exc),
                    "length": str(len(hex_value)),
                    "preview": hex_value[:64],
                }
            )
    return issues


def inspect_set(set_path: Path) -> dict[str, Any]:
    with gzip.open(set_path, "rb") as handle:
        root = ET.parse(handle).getroot()

    report: dict[str, Any] = {
        "set_path": str(set_path),
        "tracks": [],
    }
    tracks_node = root.find("./LiveSet/Tracks")
    if tracks_node is None:
        return report
    for track in tracks_node:
        if strip_tag(track.tag) not in TRACK_TAGS:
            continue
        track_name = find_track_name(track)
        track_report: dict[str, Any] = {
            "track_tag": strip_tag(track.tag),
            "track_name": track_name,
            "plugins": [],
        }
        for plugin in track.findall(".//PluginDevice"):
            browser = plugin_browser_path(plugin)
            branch = plugin_branch_id(plugin)
            if any(match in browser or match in branch for match in ("ValhallaPlate", "Endless%20Smile", "Gullfoss", "Kontakt", "EQPB", "API%202500", "Attacker", "LX480", "SplitEQ")):
                track_report["plugins"].append(plugin_summary(plugin))
        if track_report["plugins"]:
            report["tracks"].append(track_report)
    return report


def migrate_set(set_path: Path, backup_suffix: str) -> dict[str, Any]:
    backup_path = set_path.with_name(set_path.name + backup_suffix)
    shutil.copy2(set_path, backup_path)

    with gzip.open(set_path, "rb") as handle:
        tree = ET.parse(handle)
    root = tree.getroot()

    changes: dict[str, Any] = {
        "set_path": str(set_path),
        "backup_path": str(backup_path),
        "migrated": [],
        "removed": [],
    }

    tracks_node = root.find("./LiveSet/Tracks")
    if tracks_node is None:
        raise RuntimeError("LiveSet/Tracks not found in set")

    for track in tracks_node:
        track_tag = strip_tag(track.tag)
        if track_tag not in TRACK_TAGS:
            continue
        track_name = find_track_name(track)
        for devices_parent in track.findall(".//Devices"):
            for plugin in list(devices_parent):
                if strip_tag(plugin.tag) != "PluginDevice":
                    continue
                browser = plugin_browser_path(plugin)
                branch = plugin_branch_id(plugin)
                raw_vst2 = hex_text(plugin.find("./PluginDesc/VstPluginInfo/Preset/VstPreset/Buffer"))

                if browser in MIGRATION_SPECS and raw_vst2:
                    spec = MIGRATION_SPECS[browser]
                    xml_attributes = extract_xml_attributes_from_hex(raw_vst2)
                    source_context = plugin.find("./SourceContext/Value/BranchSourceContext")
                    if source_context is not None:
                        browser_node = source_context.find("./BrowserContentPath")
                        branch_node = source_context.find("./BranchDeviceId")
                        if browser_node is not None:
                            set_value(browser_node, spec.browser_path)
                        if branch_node is not None:
                            set_value(branch_node, spec.branch_device_id)

                    new_desc = build_vst3_plugin_desc(spec, plugin, raw_vst2)
                    replace_plugin_desc(plugin, new_desc)
                    remapped_parameter_ids = remap_parameter_ids(plugin, spec)
                    synced_manual_values = sync_manual_values_from_attributes(plugin, xml_attributes)
                    changes["migrated"].append(
                        {
                            "track_name": track_name,
                            "track_tag": track_tag,
                            "plugin_name": spec.plugin_name,
                            "remapped_parameter_ids": remapped_parameter_ids,
                            "synced_manual_values": synced_manual_values,
                        }
                    )
                    continue

                if any(match in browser or match in branch for match in REMOVE_BROWSER_MATCHES):
                    target_ids = collect_target_ids(plugin)
                    removed_envelopes = remove_automation_refs(track, target_ids)
                    devices_parent.remove(plugin)
                    changes["removed"].append(
                        {
                            "track_name": track_name,
                            "track_tag": track_tag,
                            "browser_path": browser,
                            "branch_device_id": branch,
                            "removed_automation_envelopes": removed_envelopes,
                        }
                    )

    indent_xml(root)
    issues = validate_hex_nodes(root)
    if issues:
        raise RuntimeError(f"hex validation failed: {json.dumps(issues[:10], ensure_ascii=False)}")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with gzip.open(set_path, "wb") as handle:
        handle.write(xml_bytes)

    # Re-open to ensure the rewritten file is structurally sound.
    with gzip.open(set_path, "rb") as handle:
        reparsed_root = ET.parse(handle).getroot()
    issues = validate_hex_nodes(reparsed_root)
    if issues:
        raise RuntimeError(f"hex validation failed after write: {json.dumps(issues[:10], ensure_ascii=False)}")

    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and migrate Ableton .als files.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect plugin state in an .als file.")
    inspect_parser.add_argument("--set", required=True, type=Path, help="Path to the .als file.")
    inspect_parser.add_argument("--output", type=Path, help="Optional JSON output path.")

    migrate_parser = subparsers.add_parser("migrate", help="Apply the requested migrations/removals.")
    migrate_parser.add_argument("--set", required=True, type=Path, help="Path to the .als file.")
    migrate_parser.add_argument(
        "--backup-suffix",
        default=".pre_codex_migration.bak",
        help="Suffix appended to the backup copy.",
    )
    migrate_parser.add_argument("--output", type=Path, help="Optional JSON output path.")

    args = parser.parse_args()

    if args.command == "inspect":
        report = inspect_set(args.set)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        print(payload)
        return

    if args.command == "migrate":
        report = migrate_set(args.set, args.backup_suffix)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        print(payload)
        return

    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
