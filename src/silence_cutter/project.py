"""Conservative reader for Kdenlive's MLT/XML project structure."""

from __future__ import annotations

import json
import copy
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class UnsupportedProject(ValueError):
    """Raised when safe timeline editing cannot be guaranteed."""


@dataclass(frozen=True)
class Track:
    index: int
    tractor_id: str
    playlist_ids: tuple[str, ...]
    name: str
    is_audio: bool = False


class KdenliveProject:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self.tree = ET.parse(self.path)
        except (ET.ParseError, OSError) as exc:
            raise UnsupportedProject(f"Cannot read project XML: {exc}") from exc
        self.root = self.tree.getroot()
        if self.root.tag != "mlt":
            raise UnsupportedProject("Expected an MLT <mlt> project root")
        self.by_id = {e.get("id"): e for e in self.root.iter() if e.get("id")}
        self.main_bin = next((e for e in self.root.findall("playlist")
                              if e.get("id") == "main_bin"), None)
        self.sequence = self._active_sequence()

    @staticmethod
    def _properties(element: ET.Element) -> dict[str, str]:
        return {p.get("name", ""): p.text or "" for p in element.findall("property")}

    def _active_sequence(self) -> ET.Element:
        """Resolve Kdenlive 23.04+ wrapper tractors to the actual sequence tractor.

        Current projects end with a project tractor whose track references the
        active sequence. Older single-sequence projects may use the sequence
        tractor itself as the last tractor.
        """
        tractors = self.root.findall("tractor")
        if not tractors:
            raise UnsupportedProject("Project contains no timeline tractor")
        wrapper = next((t for t in reversed(tractors)
                        if self._properties(t).get("kdenlive:projectTractor") == "1"), None)
        if wrapper is None:
            wrapper = tractors[-1]
        for track in wrapper.findall("track"):
            candidate = self.by_id.get(track.get("producer", ""))
            if candidate is not None and candidate.tag == "tractor" \
                    and self._properties(candidate).get("kdenlive:uuid"):
                return candidate
        if self._properties(wrapper).get("kdenlive:uuid"):
            return wrapper
        # Some older generations do not mark the wrapper with projectTractor.
        for candidate in reversed(tractors):
            if self._properties(candidate).get("kdenlive:uuid"):
                return candidate
        raise UnsupportedProject("Could not identify the active Kdenlive timeline sequence")

    def group_metadata(self) -> object:
        # In current Kdenlive formats timeline groups live on the sequence;
        # older project files stored them as a document property in main_bin.
        sequence_props = self._properties(self.sequence)
        for key in ("kdenlive:sequenceproperties.groups", "kdenlive:groups"):
            value = sequence_props.get(key, "").strip()
            if value:
                try:
                    return json.loads(value)
                except json.JSONDecodeError as exc:
                    raise UnsupportedProject("Malformed Kdenlive sequence group metadata") from exc
        if self.main_bin is None:
            return []
        prop = next((p for p in self.main_bin.findall("property")
                     if p.get("name") == "kdenlive:docproperties.groups"), None)
        if prop is None or not (prop.text or "").strip():
            return []
        try:
            return json.loads(prop.text)
        except json.JSONDecodeError as exc:
            raise UnsupportedProject("Malformed Kdenlive clip group metadata") from exc

    def _groups_property(self) -> ET.Element | None:
        for owner in (self.sequence, self.main_bin):
            if owner is None:
                continue
            prop = next((p for p in owner.findall("property")
                         if p.get("name") in {"kdenlive:sequenceproperties.groups",
                                               "kdenlive:docproperties.groups"}), None)
            if prop is not None and (prop.text or "").strip():
                return prop
        return None

    @staticmethod
    def _expand_group_node(node: object, positions: dict[tuple[int, int], list[int]]) -> list[object]:
        """Duplicate group trees for each retained fragment, remapping leaf positions.

        Kdenlive group leaves use `track:timeline-position:subLayer`; they do
        not store clip ids or playlist-entry indexes.
        """
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise UnsupportedProject("Malformed Kdenlive group node")
        if node.get("type") == "Leaf":
            try:
                track_s, pos_s, sub_s = node["data"].split(":", 2)
                key = (int(track_s), int(pos_s))
            except (KeyError, ValueError, AttributeError) as exc:
                raise UnsupportedProject("Malformed Kdenlive group leaf reference") from exc
            mapped = positions.get(key)
            if mapped is None:
                raise UnsupportedProject(
                    f"Cannot map grouped timeline clip at track {key[0]}, frame {key[1]}"
                )
            return [dict(node, data=f"{key[0]}:{new_pos}:{sub_s}") for new_pos in mapped]

        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise UnsupportedProject("Malformed Kdenlive group children")
        expanded_children = [KdenliveProject._expand_group_node(child, positions)
                             for child in children]
        counts = {len(items) for items in expanded_children}
        if len(counts) != 1:
            raise UnsupportedProject(
                "Grouped clips do not produce matching retained fragments; refusing unsafe group rewrite"
            )
        count = counts.pop()
        if node.get("type") == "AVSplit":
            if len(children) != 2 or any(child.get("type") != "Leaf" for child in children):
                raise UnsupportedProject("AVSplit groups must contain exactly two clip leaves")
        return [dict(node, children=[items[index] for items in expanded_children])
                for index in range(count)]

    def tracks(self) -> list[Track]:
        """Identify timeline tracks directly inside the active sequence tractor."""
        tracks: list[Track] = []
        for item in self.sequence.findall("track"):
            producer_id = item.get("producer", "")
            producer = self.by_id.get(producer_id)
            if producer is None or producer.tag != "tractor":
                continue
            child_ids = tuple(t.get("producer", "") for t in producer.findall("track"))
            playlists = tuple(pid for pid in child_ids
                              if self.by_id.get(pid) is not None
                              and self.by_id[pid].tag == "playlist")
            if not playlists:
                continue
            props = self._properties(producer)
            name = (props.get("kdenlive:track_name") or props.get("kdenlive:trackname")
                    or f"Track {len(tracks) + 1}")
            is_audio = props.get("kdenlive:audio_track") == "1"
            tracks.append(Track(len(tracks), producer_id, playlists, name, is_audio))
        if not tracks:
            raise UnsupportedProject("No supported timeline tracks found in active sequence")
        return tracks

    def track_media_sources(self, track: Track) -> list[Path]:
        """Return distinct file resources used by entries on a timeline track."""
        sources: list[Path] = []
        seen: set[str] = set()
        for playlist_id in track.playlist_ids:
            playlist = self.by_id[playlist_id]
            for entry in playlist.findall("entry"):
                producer = self.by_id.get(entry.get("producer", ""))
                if producer is None:
                    continue
                props = self._properties(producer)
                resource = props.get("resource") or props.get("kdenlive:originalurl")
                if not resource or resource in {"black", "color", "0"}:
                    continue
                path = Path(resource).expanduser()
                if not path.is_absolute():
                    path = self.path.parent / path
                path = path.resolve()
                if str(path) not in seen:
                    sources.append(path)
                    seen.add(str(path))
        return sources

    def source_time_at_timeline(self, track: Track, media: str | Path,
                                timeline_frame: int) -> float | None:
        """Map one timeline position back to the selected track's source time."""
        fps = self.frame_rate()
        selected = Path(media).expanduser()
        if not selected.is_absolute():
            selected = self.path.parent / selected
        selected = selected.resolve()
        for playlist_id in track.playlist_ids:
            playlist = self.by_id[playlist_id]
            cursor = 0
            for node in list(playlist):
                if node.tag == "entry":
                    if not node.get("in") or not node.get("out"):
                        continue
                    source_in = round(self._seconds(node.get("in")) * fps)
                    source_out = round(self._seconds(node.get("out")) * fps)
                    length = source_out - source_in + 1
                    producer = self.by_id.get(node.get("producer", ""))
                    props = self._properties(producer) if producer is not None else {}
                    resource = props.get("resource") or props.get("kdenlive:originalurl")
                    if resource:
                        path = Path(resource).expanduser()
                        if not path.is_absolute():
                            path = self.path.parent / path
                        if path.resolve() == selected and cursor <= timeline_frame < cursor + length:
                            return (source_in + timeline_frame - cursor) / fps
                    cursor += length
                elif node.tag == "blank" and node.get("length"):
                    cursor += round(self._seconds(node.get("length")) * fps)
        return None

    def source_cut_intervals(self, track: Track, media: str | Path,
                             timeline_cuts: list[tuple[int, int]]) -> list[tuple[float, float]]:
        """Map project cuts back onto the selected track's media-time waveform."""
        fps = self.frame_rate()
        selected = Path(media).expanduser()
        if not selected.is_absolute():
            selected = self.path.parent / selected
        selected = selected.resolve()
        source_ranges: list[tuple[int, int]] = []
        for playlist_id in track.playlist_ids:
            cursor = 0
            playlist = self.by_id[playlist_id]
            for node in list(playlist):
                if node.tag == "entry":
                    if not node.get("in") or not node.get("out"):
                        continue
                    source_in = round(self._seconds(node.get("in")) * fps)
                    source_out = round(self._seconds(node.get("out")) * fps)
                    duration = source_out - source_in + 1
                    producer = self.by_id.get(node.get("producer", ""))
                    props = self._properties(producer) if producer is not None else {}
                    resource = props.get("resource") or props.get("kdenlive:originalurl")
                    if resource:
                        path = Path(resource).expanduser()
                        if not path.is_absolute():
                            path = self.path.parent / path
                        if path.resolve() == selected:
                            for cut_start, cut_end in timeline_cuts:
                                left = max(cursor, cut_start)
                                right = min(cursor + duration, cut_end)
                                if right > left:
                                    source_ranges.append((source_in + left - cursor,
                                                          source_in + right - cursor))
                    cursor += duration
                elif node.tag == "blank" and node.get("length"):
                    cursor += round(self._seconds(node.get("length")) * fps)
        source_ranges.sort()
        merged: list[list[int]] = []
        for left, right in source_ranges:
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        return [(left / fps, right / fps) for left, right in merged]

    def frame_rate(self) -> float:
        profile = self.root.find("profile")
        if profile is None:
            raise UnsupportedProject("Project has no MLT profile")
        numerator = int(profile.get("frame_rate_num", "0"))
        denominator = int(profile.get("frame_rate_den", "1"))
        if numerator <= 0 or denominator <= 0:
            raise UnsupportedProject("Invalid project frame rate")
        return numerator / denominator

    def _project_wrapper(self) -> ET.Element:
        for tractor in reversed(self.root.findall("tractor")):
            if self._properties(tractor).get("kdenlive:projectTractor") == "1":
                return tractor
        sequence_id = self.sequence.get("id")
        for tractor in reversed(self.root.findall("tractor")):
            if any(track.get("producer") == sequence_id for track in tractor.findall("track")):
                return tractor
        raise UnsupportedProject("Cannot find a Kdenlive project tractor for the active sequence")

    def _allocate_id(self, old_id: str, used: set[str], counters: dict[str, int]) -> str:
        match = re.fullmatch(r"([A-Za-z_-]+)(\d+)", old_id)
        prefix = match.group(1) if match else f"{old_id}_copy_"
        number = counters.get(prefix, 0)
        while True:
            candidate = f"{prefix}{number}"
            number += 1
            if candidate not in used:
                counters[prefix] = number
                used.add(candidate)
                return candidate

    def clone_active_sequence(self) -> str:
        """Add a cloned cut sequence to the project and make it the active sequence.

        The original timeline is left intact. This is supported for Kdenlive's
        multi-sequence project layout, which has a main_bin and project wrapper.
        """
        if self.main_bin is None:
            raise UnsupportedProject(
                "Creating a second timeline requires a Kdenlive multi-sequence project with main_bin"
            )
        wrapper = self._project_wrapper()
        sequence_props = self._properties(self.sequence)
        if (self._properties(wrapper).get("kdenlive:projectTractor") != "1"
                or "kdenlive:sequenceproperties.tracksCount" not in sequence_props):
            raise UnsupportedProject(
                "Adding a silence-cut sequence requires Kdenlive's current multi-sequence project format"
            )
        if (self.group_metadata() and
                "kdenlive:sequenceproperties.groups" not in sequence_props):
            raise UnsupportedProject(
                "Timeline groups are stored as document-level metadata in this project format; "
                "refusing to alter the original sequence"
            )
        original_id = self.sequence.get("id")
        wrapper_tracks = [track for track in wrapper.findall("track")
                          if track.get("producer") == original_id]
        if len(wrapper_tracks) != 1:
            raise UnsupportedProject("Expected one active-sequence reference in the project tractor")
        tracks = self.tracks()
        if not tracks:
            raise UnsupportedProject("The active sequence has no cloneable timeline tracks")
        original_bin_entry = next((entry for entry in self.main_bin.findall("entry")
                                   if entry.get("producer") == original_id), None)
        if original_bin_entry is None:
            raise UnsupportedProject("The active sequence is missing from main_bin")

        used = set(self.by_id)
        counters: dict[str, int] = {}
        id_map: dict[str, str] = {}
        cloned_playlists: list[ET.Element] = []
        cloned_tracks: list[ET.Element] = []

        # Duplicate each active track's playlists and tractor. Media producers
        # remain shared bin references; only timeline containers receive new IDs.
        for track in tracks:
            for playlist_id in track.playlist_ids:
                original = self.by_id[playlist_id]
                cloned = copy.deepcopy(original)
                cloned_playlists.append(cloned)
                id_map[playlist_id] = self._allocate_id(playlist_id, used, counters)
            original_track = self.by_id[track.tractor_id]
            cloned_track = copy.deepcopy(original_track)
            cloned_tracks.append(cloned_track)
            id_map[track.tractor_id] = self._allocate_id(track.tractor_id, used, counters)
        cloned_sequence = copy.deepcopy(self.sequence)
        id_map[original_id] = self._allocate_id(original_id, used, counters)

        clones = [*cloned_playlists, *cloned_tracks, cloned_sequence]
        for element in (child for clone in clones for child in clone.iter()):
            old_id = element.get("id")
            if old_id and old_id not in id_map:
                id_map[old_id] = self._allocate_id(old_id, used, counters)
        for clone in clones:
            for element in clone.iter():
                if element.get("id") in id_map:
                    element.set("id", id_map[element.get("id")])
                producer = element.get("producer")
                if producer in id_map:
                    element.set("producer", id_map[producer])

        sequence_props = self._properties(cloned_sequence)
        original_name = sequence_props.get("kdenlive:clipname") or "Sequence"
        name = f"{original_name} - Silence Cut"
        existing_names = {self._properties(item).get("kdenlive:clipname")
                          for item in self.root.findall("tractor")}
        suffix = 2
        base_name = name
        while name in existing_names:
            name = f"{base_name} ({suffix})"
            suffix += 1
        new_uuid = "{" + str(uuid4()) + "}"
        for prop in cloned_sequence.findall("property"):
            key = prop.get("name", "")
            if key in {"kdenlive:uuid", "kdenlive:control_uuid"}:
                prop.text = new_uuid
            elif key == "kdenlive:clipname":
                prop.text = name
        sequence_bin_id = max((int(prop.text) for prop in self.root.iter("property")
                               if prop.get("name") == "kdenlive:id" and (prop.text or "").isdigit()),
                              default=0) + 1
        id_property = next((prop for prop in cloned_sequence.findall("property")
                            if prop.get("name") == "kdenlive:id"), None)
        if id_property is not None:
            id_property.text = str(sequence_bin_id)

        # Keep the active project tractor as the final MLT object. Kdenlive
        # reads the sequence through main_bin and that wrapper reference.
        insertion = list(self.root).index(self.sequence)
        for clone in cloned_playlists:
            self.root.insert(insertion, clone)
            insertion += 1
        for clone in cloned_tracks:
            self.root.insert(insertion, clone)
            insertion += 1
        self.root.insert(insertion, cloned_sequence)

        wrapper_tracks[0].set("producer", id_map[original_id])
        bin_entry = copy.deepcopy(original_bin_entry)
        bin_entry.set("producer", id_map[original_id])
        self.main_bin.append(bin_entry)

        self.sequence = cloned_sequence
        self.by_id = {element.get("id"): element for element in self.root.iter()
                      if element.get("id")}
        return name

    def plan_timeline_cuts(self, media: str | Path,
                           silences: list[tuple[float, float]]) -> list[tuple[int, int]]:
        """Map media-relative silence intervals to merged project-frame ranges."""
        fps = self.frame_rate()
        media_path = Path(media).expanduser()
        if not media_path.is_absolute():
            media_path = self.path.parent / media_path
        media_path = media_path.resolve()

        def resource_for_entry(entry: ET.Element) -> str | None:
            producer = self.by_id.get(entry.get("producer", ""))
            if producer is None:
                return None
            props = self._properties(producer)
            resource = props.get("resource") or props.get("kdenlive:originalurl")
            if not resource or resource in {"0", "black", "color"}:
                return None
            candidate = Path(resource).expanduser()
            if not candidate.is_absolute():
                candidate = self.path.parent / candidate
            return str(candidate.resolve())

        cuts: list[tuple[int, int]] = []
        active_playlist_ids = {playlist_id for track in self.tracks()
                               for playlist_id in track.playlist_ids}
        for playlist in self.root.findall("playlist"):
            if playlist.get("id") not in active_playlist_ids:
                continue
            cursor = 0
            for node in list(playlist):
                if node.tag == "entry":
                    if not node.get("in") or not node.get("out"):
                        raise UnsupportedProject("Timeline entry is missing source in/out points")
                    source_in = round(self._seconds(node.get("in")) * fps)
                    source_out = round(self._seconds(node.get("out")) * fps)
                    duration = source_out - source_in + 1
                    if duration <= 0:
                        raise UnsupportedProject("Timeline entry has invalid source range")
                    if resource_for_entry(node) == str(media_path):
                        for start_s, end_s in silences:
                            left = round(start_s * fps) - source_in + cursor
                            right = round(end_s * fps) - source_in + cursor
                            left, right = max(cursor, left), min(cursor + duration, right)
                            if right > left:
                                cuts.append((left, right))
                    cursor += duration
                elif node.tag == "blank":
                    length = node.get("length")
                    if not length:
                        raise UnsupportedProject("Timeline blank is missing its length")
                    cursor += round(self._seconds(length) * fps)
        cuts.sort()
        merged: list[list[int]] = []
        for left, right in cuts:
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        return [(left, right) for left, right in merged]

    def write_copy(self, output: str | Path) -> None:
        output = Path(output)
        if output.resolve() == self.path.resolve():
            raise ValueError("Refusing to overwrite the original project")
        self.tree.write(output, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _seconds(value: str) -> float:
        parts = value.split(":")
        if len(parts) != 3:
            raise UnsupportedProject(f"Unsupported MLT time value: {value!r}")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

    @staticmethod
    def _time(seconds: float) -> str:
        # MLT timestamps are millisecond-based; values are frame-aligned before
        # reaching this formatter.
        millis = max(0, round(seconds * 1000))
        hours, rem = divmod(millis, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        secs, ms = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"

    def apply_timeline_cuts(self, media: str | Path, silences: list[tuple[float, float]],
                            output: str | Path | None = None) -> int:
        """Add a cut version as a new sequence and save the project.

        Kdenlive group leaves are remapped by their documented track and
        timeline-position coordinates. Groups are duplicated when all member
        clips split into the same number of retained fragments; ambiguous or
        asymmetric group edits are rejected before writing. The input sequence
        remains unchanged. By default, save back to the input project; pass a
        different output path to create a separate project file.
        """
        self.clone_active_sequence()
        groups_prop = self._groups_property()
        groups = self.group_metadata()
        if not isinstance(groups, list):
            raise UnsupportedProject("Kdenlive group metadata must be a JSON array")
        fps = self.frame_rate()
        cut_ranges = self.plan_timeline_cuts(media, silences)
        if not cut_ranges:
            raise UnsupportedProject("No detected silence overlaps a timeline instance of the selected media")

        playlist_track: dict[str, int] = {}
        for timeline_track in self.tracks():
            for playlist_id in timeline_track.playlist_ids:
                playlist_track[playlist_id] = timeline_track.index
        group_positions: dict[tuple[int, int], list[int]] = {}

        # Playlist-level ripple edit. Every timeline playlist receives the same
        # time ranges, preserving inter-track alignment and source offsets.
        active_tracks = self.tracks()
        active_playlist_ids = {playlist_id for track in active_tracks
                               for playlist_id in track.playlist_ids}
        for playlist in self.root.findall("playlist"):
            if playlist.get("id") not in active_playlist_ids:
                continue
            old_nodes = list(playlist)
            new_nodes = [n for n in old_nodes if n.tag == "property"]
            cursor = 0
            track_index = playlist_track.get(playlist.get("id", ""))
            for node in old_nodes:
                if node.tag == "property":
                    continue
                if node.tag not in {"entry", "blank"}:
                    raise UnsupportedProject(f"Unsupported playlist element <{node.tag}>")
                if node.tag == "entry":
                    src_in = round(self._seconds(node.get("in", "00:00:00.000")) * fps)
                    src_out = round(self._seconds(node.get("out", "00:00:00.000")) * fps)
                    duration = src_out - src_in + 1
                else:
                    duration = round(self._seconds(node.get("length", "00:00:00.000")) * fps)
                end = cursor + duration
                pieces: list[tuple[int, int]] = []
                pos = cursor
                for a, b in cut_ranges:
                    if b <= pos or a >= end:
                        continue
                    if a > pos:
                        pieces.append((pos, min(a, end)))
                    pos = max(pos, min(b, end))
                if pos < end:
                    pieces.append((pos, end))
                for a, b in pieces:
                    if b <= a:
                        continue
                    if node.tag == "entry":
                        clone = copy.deepcopy(node)
                        clone.set("in", self._time((src_in + a - cursor) / fps))
                        clone.set("out", self._time((src_in + b - cursor - 1) / fps))
                        new_nodes.append(clone)
                        if track_index is not None:
                            old_start = cursor
                            new_start = a - sum(max(0, min(a, cut_b) - cut_a)
                                                for cut_a, cut_b in cut_ranges)
                            group_positions.setdefault((track_index, old_start), []).append(new_start)
                    else:
                        clone = copy.deepcopy(node)
                        clone.set("length", self._time((b - a) / fps))
                        new_nodes.append(clone)
                cursor = end
            playlist[:] = new_nodes

        # Convert old group leaf positions into the positions of retained clip
        # fragments. Each resulting fragment receives a copy of its original
        # group hierarchy, preserving AVSplit's two-member invariant.
        if groups_prop is not None:
            updated_groups: list[object] = []
            for group in groups:
                updated_groups.extend(self._expand_group_node(group, group_positions))
            groups_prop.text = json.dumps(updated_groups, indent=2)

        # Keep sequence and wrapper duration metadata in sync with the ripple.
        removed = sum(b - a for a, b in cut_ranges) / fps
        wrapper = self._project_wrapper()
        affected_tractors = {track.tractor_id for track in active_tracks}
        affected_tractors.add(self.sequence.get("id", ""))
        affected_tractors.add(wrapper.get("id", ""))
        for element_id in affected_tractors:
            element = self.by_id.get(element_id)
            if element is not None and element.tag == "tractor" and element.get("out"):
                old_out = self._seconds(element.get("out"))
                element.set("out", self._time(max(0.0, old_out - removed)))
        duration_prop = next((prop for prop in self.sequence.findall("property")
                              if prop.get("name") == "kdenlive:duration" and prop.text), None)
        if duration_prop is not None:
            new_duration = self._time(max(0.0, self._seconds(duration_prop.text) - removed))
            duration_prop.text = new_duration
            for entry in self.main_bin.findall("entry"):
                if entry.get("producer") == self.sequence.get("id"):
                    entry.set("out", new_duration)
            for track in wrapper.findall("track"):
                if track.get("producer") == self.sequence.get("id") and track.get("out"):
                    track.set("out", self._time(max(0.0, self._seconds(track.get("out")) - removed)))

        output_path = Path(output) if output is not None else self.path
        output_path = output_path.resolve()
        in_place = output_path == self.path.resolve()
        if output_path.exists() and not in_place:
            raise ValueError(f"Output already exists; refusing to overwrite it: {output_path}")
        self._write_atomically(output_path)
        return round(removed * fps)

    def _write_atomically(self, output_path: Path) -> None:
        """Write XML to a sibling temporary file and atomically replace the target."""
        original_mode = None
        if output_path.exists():
            original_mode = stat.S_IMODE(output_path.stat().st_mode)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                self.tree.write(stream, encoding="utf-8", xml_declaration=True)
                stream.flush()
                os.fsync(stream.fileno())
            if original_mode is not None:
                os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, output_path)
        except BaseException:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise
