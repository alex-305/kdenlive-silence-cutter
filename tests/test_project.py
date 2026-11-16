from pathlib import Path
import json
import tempfile
import unittest
import xml.etree.ElementTree as ETree

from silence_cutter.project import KdenliveProject, UnsupportedProject


class ProjectTests(unittest.TestCase):
    def test_rejects_non_mlt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.kdenlive"
            source.write_text("<root/>")
            with self.assertRaisesRegex(UnsupportedProject, "MLT"):
                KdenliveProject(source)

    def test_never_overwrites_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "p.kdenlive"
            source.write_text('<mlt><playlist id="main_bin"/><tractor id="sequence">'
                              '<property name="kdenlive:uuid">seq</property></tractor></mlt>')
            project = KdenliveProject(source)
            with self.assertRaisesRegex(ValueError, "original"):
                project.write_copy(source)

    def test_resolves_current_format_active_sequence_and_group_metadata(self):
        xml = '''<mlt>
          <playlist id="main_bin"><entry producer="sequence" in="00:00:00.000" out="00:00:05.000"/></playlist>
          <playlist id="video_a"/><playlist id="video_b"/>
          <playlist id="audio_a"/><playlist id="audio_b"/>
          <producer id="bin_media"/>
          <tractor id="video_track"><property name="kdenlive:track_name">Video</property>
            <track producer="video_a"/><track producer="video_b"/></tractor>
          <tractor id="audio_track"><property name="kdenlive:track_name">Audio</property>
            <track producer="audio_a"/><track producer="audio_b"/></tractor>
          <tractor id="sequence">
            <property name="kdenlive:uuid">sequence-uuid</property>
            <property name="kdenlive:sequenceproperties.groups">[{"type":"AVSplit"}]</property>
            <track producer="bin_media"/><track producer="video_track"/><track producer="audio_track"/>
          </tractor>
          <tractor id="project_wrapper">
            <property name="kdenlive:projectTractor">1</property>
            <track producer="sequence"/>
          </tractor>
        </mlt>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "current.kdenlive"
            source.write_text(xml)
            project = KdenliveProject(source)
            self.assertEqual([t.tractor_id for t in project.tracks()], ["video_track", "audio_track"])
            self.assertEqual([t.name for t in project.tracks()], ["Video", "Audio"])
            self.assertEqual(project.group_metadata(), [{"type": "AVSplit"}])

    def test_ripple_writes_copy_and_splits_entries_on_every_track(self):
        xml = '''<mlt>
          <profile frame_rate_num="25" frame_rate_den="1"/>
          <playlist id="main_bin"><entry producer="sequence" in="00:00:00.000" out="00:00:05.000"/></playlist>
          <chain id="mic"><property name="resource">mic.wav</property></chain>
          <chain id="video"><property name="resource">video.mp4</property></chain>
          <playlist id="p0"><entry producer="mic" in="00:00:00.000" out="00:00:04.960"/></playlist>
          <playlist id="p1"><entry producer="video" in="00:00:10.000" out="00:00:14.960"/></playlist>
          <tractor id="t0"><track producer="p0"/></tractor>
          <tractor id="t1"><track producer="p1"/></tractor>
          <tractor id="sequence" in="00:00:00.000" out="00:00:04.960">
            <property name="kdenlive:uuid">seq</property>
            <property name="kdenlive:clipname">Sequence 1</property>
            <property name="kdenlive:id">3</property>
            <property name="kdenlive:duration">00:00:05.000</property>
            <property name="kdenlive:sequenceproperties.tracksCount">2</property>
            <property name="kdenlive:sequenceproperties.groups">[{"type":"AVSplit","children":[{"type":"Leaf","leaf":"clip","data":"0:0:-1"},{"type":"Leaf","leaf":"clip","data":"1:0:-1"}]}]</property>
            <track producer="t0"/><track producer="t1"/>
          </tractor>
          <tractor id="wrapper"><property name="kdenlive:projectTractor">1</property>
            <track producer="sequence" in="00:00:00.000" out="00:00:04.960"/></tractor>
        </mlt>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.kdenlive"
            output = Path(tmp) / "edited.kdenlive"
            (Path(tmp) / "mic.wav").touch()
            source.write_text(xml)
            original_bytes = source.read_bytes()
            project = KdenliveProject(source)
            self.assertEqual(project.plan_timeline_cuts("mic.wav", [(1.0, 2.0)]), [(25, 50)])
            self.assertEqual(project.source_time_at_timeline(project.tracks()[1], "video.mp4", 25), 11.0)
            self.assertEqual(project.source_cut_intervals(project.tracks()[0], "mic.wav", [(25, 50)]),
                             [(1.0, 2.0)])
            removed = project.apply_timeline_cuts("mic.wav", [(1.0, 2.0)], output)
            self.assertEqual(removed, 25)
            self.assertEqual(source.read_bytes(), original_bytes)
            written = ETree.parse(output).getroot()
            all_playlists = {p.get("id"): p.findall("entry") for p in written.findall("playlist")}
            self.assertEqual(len(all_playlists["p0"]), 1)
            self.assertEqual(len(all_playlists["p1"]), 1)
            wrapper = next(t for t in written.findall("tractor") if t.get("id") == "wrapper")
            active_sequence_id = wrapper.find("track").get("producer")
            self.assertNotEqual(active_sequence_id, "sequence")
            active_sequence = next(t for t in written.findall("tractor")
                                   if t.get("id") == active_sequence_id)
            cloned_track_ids = [item.get("producer") for item in active_sequence.findall("track")]
            cloned_playlists = [child.get("producer") for track_id in cloned_track_ids
                                for child in next(t for t in written.findall("tractor")
                                                  if t.get("id") == track_id).findall("track")]
            entries = {playlist_id: all_playlists[playlist_id] for playlist_id in cloned_playlists}
            self.assertEqual(len(entries[cloned_playlists[0]]), 2)
            self.assertEqual(entries[cloned_playlists[0]][1].get("in"), "00:00:02.000")
            self.assertEqual(len(entries[cloned_playlists[1]]), 2)
            self.assertEqual(entries[cloned_playlists[1]][1].get("in"), "00:00:12.000")
            original = next(t for t in written.findall("tractor") if t.get("id") == "sequence")
            self.assertEqual(original.find("property[@name='kdenlive:clipname']").text, "Sequence 1")
            groups = next(p.text for t in written.findall("tractor") if t.get("id") == active_sequence_id
                          for p in t.findall("property")
                          if p.get("name") == "kdenlive:sequenceproperties.groups")
            decoded = json.loads(groups)
            self.assertEqual(len(decoded), 2)
            self.assertEqual([leaf["data"] for group in decoded for leaf in group["children"]],
                             ["0:0:-1", "1:0:-1", "0:25:-1", "1:25:-1"])

    def test_ripple_adds_timeline_in_place_and_preserves_original_sequence(self):
        xml = '''<mlt>
          <profile frame_rate_num="25" frame_rate_den="1"/>
          <playlist id="main_bin"><entry producer="sequence" in="00:00:00.000" out="00:00:04.960"/></playlist>
          <chain id="mic"><property name="resource">mic.wav</property></chain>
          <playlist id="p0"><entry producer="mic" in="00:00:00.000" out="00:00:04.960"/></playlist>
          <tractor id="t0"><track producer="p0"/></tractor>
          <tractor id="sequence" in="00:00:00.000" out="00:00:04.960">
            <property name="kdenlive:uuid">seq</property>
            <property name="kdenlive:clipname">Original</property>
            <property name="kdenlive:id">3</property>
            <property name="kdenlive:duration">00:00:05.000</property>
            <property name="kdenlive:sequenceproperties.tracksCount">1</property>
            <track producer="t0"/>
          </tractor>
          <tractor id="wrapper"><property name="kdenlive:projectTractor">1</property>
            <track producer="sequence" in="00:00:00.000" out="00:00:04.960"/></tractor>
        </mlt>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.kdenlive"
            (Path(tmp) / "mic.wav").touch()
            source.write_text(xml)

            project = KdenliveProject(source)
            removed = project.apply_timeline_cuts("mic.wav", [(1.0, 2.0)])

            self.assertEqual(removed, 25)
            written = ETree.parse(source).getroot()
            playlists = {playlist.get("id"): playlist.findall("entry")
                         for playlist in written.findall("playlist")}
            self.assertEqual(len(playlists["p0"]), 1)
            self.assertEqual(playlists["p0"][0].get("in"), "00:00:00.000")
            wrapper = next(tractor for tractor in written.findall("tractor")
                           if tractor.get("id") == "wrapper")
            active_sequence_id = wrapper.find("track").get("producer")
            self.assertNotEqual(active_sequence_id, "sequence")
            active_sequence = next(tractor for tractor in written.findall("tractor")
                                   if tractor.get("id") == active_sequence_id)
            cloned_track_id = active_sequence.find("track").get("producer")
            cloned_playlist_id = next(tractor for tractor in written.findall("tractor")
                                      if tractor.get("id") == cloned_track_id).find("track").get("producer")
            self.assertEqual(len(playlists[cloned_playlist_id]), 2)
            original = next(tractor for tractor in written.findall("tractor")
                            if tractor.get("id") == "sequence")
            self.assertEqual(original.find("property[@name='kdenlive:clipname']").text, "Original")
            self.assertEqual(list(Path(tmp).glob(".source.kdenlive.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
