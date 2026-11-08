#!/usr/bin/env python3
"""Offline regression tests for automatic map detection."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path


RESEARCH_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(RESEARCH_DIRECTORY))

import detect_current_map as detector  # noqa: E402


class MapDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.environ.get('TW_MAPTRIGGERS_TEST_CATALOG'):
            raise unittest.SkipTest('Set TW_MAPTRIGGERS_TEST_CATALOG to a locally generated catalog for disc-data checks')
        cls.catalog_path = Path(os.environ['TW_MAPTRIGGERS_TEST_CATALOG'])
        cls.catalog = json.loads(
            cls.catalog_path.read_text(encoding="utf-8")
        )

    def dataset_memory(self, item: dict) -> bytes:
        dataset = json.loads(
            (self.catalog_path.parent / item["dataset"]).read_text(
                encoding="utf-8"
            )
        )
        return b"\0".join(
            volume["name"].encode("latin-1", errors="ignore")
            for volume in dataset["volumes"]
        )

    def test_every_primary_dataset_is_detectable_from_its_names(self) -> None:
        failures: list[str] = []
        for item in self.catalog["entries"]:
            if item["category"] == "auxiliary":
                continue
            results = detector.scan(
                self.dataset_memory(item),
                self.catalog,
                self.catalog_path.parent,
                None,
            )
            detected, reason = detector.confident_detection(results)
            if detected is None or detected["id"] != item["id"]:
                actual = "<ambiguous>" if detected is None else detected["id"]
                failures.append(
                    f"{item['id']} -> {actual}: {reason}"
                )
        self.assertEqual([], failures)

    def test_single_volume_stage_can_be_detected(self) -> None:
        item = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level63_stage_4"
        )
        results = detector.scan(
            self.dataset_memory(item),
            self.catalog,
            self.catalog_path.parent,
            None,
        )
        detected, _reason = detector.confident_detection(results)
        self.assertIsNotNone(detected)
        self.assertEqual(item["id"], detected["id"])

    def test_level_identifier_does_not_match_longer_level_ids(self) -> None:
        memory = b"level99 LEVEL95 xlevel9 level9_suffix LEVEL9\0"
        self.assertEqual(
            1,
            detector.identifier_occurrences(memory, b"LEVEL9"),
        )
        self.assertEqual(
            0,
            detector.identifier_occurrences(memory, b"level9"),
        )
        self.assertEqual(
            -1,
            detector.identifier_find(b"vbAmbSound_10\0", b"vbAmbSound_1"),
        )

    def test_player_proximity_resolves_resident_script_tie(self) -> None:
        new_blood = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level99_new_blood"
        )
        coney = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        memory = (
            self.dataset_memory(new_blood)
            + b"\0"
            + self.dataset_memory(coney)
        )
        cases = (
            ((53.1, 41.9, 0.2), new_blood["id"]),
            ((-54.6, 6.2, -0.3), coney["id"]),
        )
        for player_position, expected_id in cases:
            with self.subTest(expected_id=expected_id):
                results = detector.scan(
                    memory,
                    self.catalog,
                    self.catalog_path.parent,
                    None,
                    player_position=player_position,
                )
                detected, reason = detector.confident_detection(results)
                self.assertIsNotNone(detected, reason)
                self.assertEqual(expected_id, detected["id"])

    def test_complete_active_mission_outweighs_coexisting_hub(self) -> None:
        payin = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level86_payin_the_cost"
        )
        coney = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        memory = (
            self.dataset_memory(payin)
            + b"\0level86\0"
            + self.dataset_memory(coney)
        )
        results = detector.scan(
            memory,
            self.catalog,
            self.catalog_path.parent,
            None,
            player_position=(-54.6, 6.2, -0.3),
        )

        detected, reason = detector.confident_detection(results)

        self.assertIsNotNone(detected, reason)
        self.assertEqual(payin["id"], detected["id"])

    def test_set_up_outweighs_resident_coney_hub(self) -> None:
        set_up = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level14_set_up"
        )
        coney = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        set_up_names = {
            "vbAmbSound_01",
            "vbAmbSound_02",
            "vbAmbSound_03",
            "vbAmbSound_04",
            "vbAmbSound_05",
            "vbAmbSound_06",
            "vbAmbSound_07",
            "vbAmbSound_08",
            "vbAmbSound_09",
            "vbAmbSound_10",
            "vSecurityTurf",
            "vClubhouse",
            "vJSBTurf",
            "vBumTurf",
            "vJSBTurf01",
            "vEndMission",
            "vCourtyard",
            "vTDTurf",
            "vPartyTurf",
            "vAptTurf",
            "vToolTurf02",
            "vToolTurf01",
        }
        set_up_dataset = json.loads(
            (self.catalog_path.parent / set_up["dataset"]).read_text(
                encoding="utf-8"
            )
        )
        coney_dataset = json.loads(
            (self.catalog_path.parent / coney["dataset"]).read_text(
                encoding="utf-8"
            )
        )
        memory_names = [
            volume["name"]
            for volume in set_up_dataset["volumes"]
            if volume["name"] in set_up_names
        ]
        memory_names.extend(
            volume["name"]
            for volume in coney_dataset["volumes"]
            if volume["name"] != "vTurfCarny"
        )
        memory = (
            b"\0".join(name.encode("latin-1") for name in memory_names)
            + b"\0level14\0LEVEL14\0"
        )
        results = detector.scan(
            memory,
            self.catalog,
            self.catalog_path.parent,
            None,
            player_position=(-23.410429, 22.007223, 0.002162),
        )

        detected, reason = detector.confident_detection(results)

        self.assertIsNotNone(detected, reason)
        self.assertEqual(set_up["id"], detected["id"])
        self.assertEqual(13, detected["distinctive_hits"])
        self.assertEqual(22, detected["all_name_hits"])

        hub = next(row for row in results if row["id"] == coney["id"])
        # With corrected box centers these datasets nearly overlap. The
        # mission preference requires that the rival hub has no level token.
        hub["level_hits"] = 1
        detected, _reason = detector.confident_detection(results)
        self.assertIsNone(detected)
        hub["level_hits"] = 0
        hub["nearest_volume_distance"] = 0.0
        detected, _reason = detector.confident_detection(results)
        self.assertIsNone(detected)

    def test_partial_large_dataset_remains_inconclusive(self) -> None:
        item = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level93_destroyed"
        )
        dataset = json.loads(
            (self.catalog_path.parent / item["dataset"]).read_text(
                encoding="utf-8"
            )
        )
        memory = dataset["volumes"][0]["name"].encode("latin-1")
        results = detector.scan(
            memory,
            self.catalog,
            self.catalog_path.parent,
            None,
        )
        detected, _reason = detector.confident_detection(results)
        self.assertIsNone(detected)

    def friendly_faces_with_resident_hub(self, hub_token: bytes = b"") -> list[dict]:
        friendly = next(
            entry for entry in self.catalog["entries"]
            if entry["id"] == "level55_friendly_faces"
        )
        coney = next(
            entry for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        # Live heap lacks vLizCam_WideDeep and vTurfCarny. The player is
        # outside every trigger, so absolute proximity cannot resolve it.
        names = self.dataset_memory(friendly).split(b"\0")
        names += self.dataset_memory(coney).split(b"\0")
        memory = b"\0".join(
            name for name in names
            if name not in {b"vLizCam_WideDeep", b"vTurfCarny"}
        ) + b"\0LEVEL55\0" + hub_token
        return detector.scan(
            memory, self.catalog, self.catalog_path.parent, None,
            player_position=(281.1390075683594, 38.2193717956543, -203.0306396484375),
        )

    def test_friendly_faces_detected_outside_trigger_boxes(self) -> None:
        for token in (b"", b"level95\0"):
            with self.subTest(hub_token=token):
                results = self.friendly_faces_with_resident_hub(token)
                detected, reason = detector.confident_detection(results)
                self.assertIsNotNone(detected, reason)
                self.assertEqual("level55_friendly_faces", detected["id"])
                self.assertEqual(13, detected["distinctive_hits"])
                self.assertEqual(14, detected["all_name_hits"])
                self.assertGreater(detected["nearest_volume_distance"], 80)

    def test_distant_mission_requires_near_complete_names_and_separation(self) -> None:
        for case in (
            "no token", "partial unique names", "partial all names",
            "unmatched nearby box", "missing matched distance",
            "nearer rival", "missing rival distance", "comparable rival distance",
            "low score",
        ):
            with self.subTest(case=case):
                results = self.friendly_faces_with_resident_hub(b"level95\0")
                mission = next(row for row in results if row["id"] == "level55_friendly_faces")
                hub = next(row for row in results if row["category"] == "hub")
                if case == "no token":
                    mission["level_hits"] = 0
                elif case == "partial unique names":
                    mission["distinctive_hits"] = 10
                elif case == "partial all names":
                    mission["all_name_hits"] = 12
                elif case == "unmatched nearby box":
                    mission["nearest_matched_volume_distance"] = 200.0
                elif case == "missing matched distance":
                    mission["nearest_matched_volume_distance"] = None
                elif case == "nearer rival":
                    hub["nearest_volume_distance"] = 50.0
                elif case == "missing rival distance":
                    hub["nearest_volume_distance"] = None
                elif case == "comparable rival distance":
                    hub["nearest_volume_distance"] = 200.0
                elif case == "low score":
                    mission["score"] = hub["score"] * 0.7
                detected, _reason = detector.confident_detection(results)
                self.assertIsNone(detected)

    def test_final_checkpoint_outweighs_resident_coney_hub(self) -> None:
        final_names = (
            b"TurfFight02", b"vFOVZoom", b"vFOVZoom01",
            b"TurfF01", b"TurfF02", b"TurfF03",
        )
        coney = next(
            entry for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        # Exact six-name subset and position observed in the live final area.
        # The full heap also contains a hub level token; it is not exclusive.
        for hub_token in (b"", b"\0level95\0"):
            with self.subTest(hub_token=hub_token):
                memory = (
                    b"\0".join(final_names) + b"\0LEVEL84\0"
                    + self.dataset_memory(coney) + hub_token
                )
                results = detector.scan(
                    memory, self.catalog, self.catalog_path.parent, None,
                    player_position=(-476.398285, -222.174469, -0.207339),
                )
                detected, reason = detector.confident_detection(results)
                self.assertIsNotNone(detected, reason)
                self.assertEqual("level84_come_out_to_play", detected["id"])
                self.assertEqual(6, detected["distinctive_hits"])
                self.assertEqual(16, detected["distinctive_total"])

    def test_partial_checkpoint_override_requires_correlated_evidence(self) -> None:
        coney = next(
            entry for entry in self.catalog["entries"]
            if entry["id"] == "level95_coney"
        )
        final_names = (
            b"TurfFight02", b"vFOVZoom", b"vFOVZoom01",
            b"TurfF01", b"TurfF02", b"TurfF03",
        )
        final_position = (-476.398285, -222.174469, -0.207339)
        cases = (
            ("missing token", final_names, b"", final_position),
            ("missing position", final_names, b"LEVEL84", None),
            ("player in hub", final_names, b"LEVEL84", (-54.6, 6.2, -0.3)),
            ("too few names", final_names[:2], b"LEVEL84", final_position),
            # The player is near an unmatched final-area volume. Matching
            # only distant chase names must not qualify the final-area box.
            ("unmatched nearby volume", (
                b"VCarChaseTrig_01", b"VCarChaseTrig_05", b"VCarChaseTrig_03",
            ), b"LEVEL84", final_position),
        )
        for label, names, token, position in cases:
            with self.subTest(label=label):
                results = detector.scan(
                    b"\0".join(names) + b"\0" + token + b"\0"
                    + self.dataset_memory(coney),
                    self.catalog, self.catalog_path.parent, None,
                    player_position=position,
                )
                detected, _reason = detector.confident_detection(results)
                self.assertNotEqual(
                    "level84_come_out_to_play",
                    detected["id"] if detected else None,
                )

    def test_volume_distance_uses_corner_plus_half_size_as_center(self) -> None:
        volume = {"origin": [10, 20, 30], "size": [4, 8, 6]}
        self.assertEqual(0, detector.point_to_volume_distance((14, 28, 36), volume))
        self.assertEqual(3, detector.point_to_volume_distance((7, 20, 30), volume))
        self.assertEqual(3, detector.point_to_volume_distance((14, 28, 39), volume))
        # A 90-degree rotation about (12, 24, 33) yields XY bounds
        # [8, 16] x [22, 26], rather than rotating about the Lua corner.
        volume["rotation_xy"] = [0, -1, 1, 0]
        self.assertEqual(0, detector.point_to_volume_distance((8, 22, 30), volume))
        self.assertEqual(3, detector.point_to_volume_distance((19, 24, 33), volume))

    def test_small_multivolume_dataset_keeps_three_name_minimum(self) -> None:
        item = next(
            entry
            for entry in self.catalog["entries"]
            if entry["id"] == "level60_stage_1"
        )
        dataset = json.loads(
            (self.catalog_path.parent / item["dataset"]).read_text(
                encoding="utf-8"
            )
        )
        memory = b"\0".join(
            volume["name"].encode("latin-1")
            for volume in dataset["volumes"][:2]
        )
        results = detector.scan(
            memory,
            self.catalog,
            self.catalog_path.parent,
            None,
        )
        detected, _reason = detector.confident_detection(results)
        self.assertIsNone(detected)


if __name__ == "__main__":
    unittest.main()
