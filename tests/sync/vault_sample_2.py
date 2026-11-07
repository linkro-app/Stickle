"""A vault as two devices of this version leave it, kept as files
(vault_sample_2/): notes with their fields, marks and reminders, one in the
trash, one emptied, a conflict copy; categories, two of one name become one;
marks, a built-in one renamed and one of the user's own become the built-in
one; colours, two alike; a note brought in; the devices' own files. Every
later version must take it in as test_vault_sample_2 says. Made once with
write_sample() and never again (vault_sample_1 is the container's sample).

    uv run python tests/sync/vault_sample_2.py <empty folder>
"""

import hashlib
import sys
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
from fake_cloud import DROPBOX, FakeCloud

from stickle.core.clock import stamp_of
from stickle.core.reminders import Rule
from stickle.core.text_reminders import due
from stickle.crypto import recovery
from stickle.crypto.vault_keys import RECOVERY_KEY, DataKeys, key_file
from stickle.data.colors import ColorRepository
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.sync.devices import About
from stickle.sync.engine import Place, sync_vault
from stickle.sync.format import KEYS, VAULT_FILE, VaultInfo, item_path, vault_file

SAMPLE_2 = Path(__file__).parent / "vault_sample_2"
DATA_KEY = bytes.fromhex("5d2e8b0c7a41f39e6b18c4d20f7a9e3315c86b2fd0a4e7193c5b8d6e2f10a947")
DATA_KEYS = DataKeys({1: DATA_KEY})
RECOVERY_KEY_TEXT = "4S6B-J8HQ-4EGS-BHCW-182C-RGF2-652K-68K6"
DATABASE_KEY = bytes.fromhex("a3c1f0e2d4b6987a5c3e1f0d2b4a69788c7e6f5d4c3b2a1908f7e6d5c4b3a291")
VAULT = "5f3a0c2e-7b14-4d9a-8e61-3c2b7a9f0d18"
LAPTOP = "2c7e4a91-0b3d-4f6e-9a18-5d2c7b4e1f03"
DESK = "8f1d3b6a-4c2e-4a97-b5d0-1e8f3c6a2b49"
NOTE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
TRASHED = "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"
EMPTIED = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
EDITED = "3d4e5f6a-7b8c-4d9e-8f1a-2b3c4d5e6f7a"
BROUGHT = "4e5f6a7b-8c9d-4e0f-9a2b-3c4d5e6f7a8b"
START = 1_793_500_000_000  # 2026-11-01, by the cloud's clock and both devices'
NINE = time(9)

# What the files are, so that one made again does not pass for the sample.
FILES_SHA256: dict[str, str] = {
    "devices/2c7e4a91-0b3d-4f6e-9a18-5d2c7b4e1f03.json": (
        "b69004c5e3650b87a08fc49b74c8fe7bf912ead1f0104b25ff56005496653b0f"
    ),
    "devices/8f1d3b6a-4c2e-4a97-b5d0-1e8f3c6a2b49.json": (
        "f8a9b118b993fdd9de62a55e72e5dbc07bd62f7bdba6fd7718544b19a26ccd83"
    ),
    "keys/2280ea5f-7be7-4353-95f9-286921dc26e2.json": (
        "a5a6a7ddffe4d5195b4c008dd8de356f869906d70922aab7748e9629ae18b4b1"
    ),
    "notes/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d.json": (
        "1052d6a1bdb5d808c1b2b40f7118e1b6bc8e36ec1125f08fd29f1658ecdae32c"
    ),
    "notes/1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e.json": (
        "5adfdb658836dd3141d608b09f6bef76b21ebe461a9d5d97cf142bb127e453ad"
    ),
    "notes/2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f.json": (
        "ebe6eee2e21a709097a437929de850fa08ef37e6c40742de64342c4005890d03"
    ),
    "notes/3d4e5f6a-7b8c-4d9e-8f1a-2b3c4d5e6f7a.json": (
        "4871f331ba124c712e70aaca7d3351d07702cd14083f94c0c7d544df14a37917"
    ),
    "notes/4e5f6a7b-8c9d-4e0f-9a2b-3c4d5e6f7a8b.json": (
        "b9564c94b6ca0d12c5e73126a3c9651a876257e54e3a4d12f65e28b305ec9046"
    ),
    "notes/592f55a3-95bb-4bd7-9276-2be9f37b6c65.json": (
        "298e1d7c608370e77eee203bff5c676535c3ac6945d1983f6a316162110177c3"
    ),
    "notes/a753d4e9-b7a2-479b-adf1-2515357b186c.json": (
        "71214d808c206452aac3461df4a4609f7cdf10abad918986369a146e0f79212e"
    ),
    "records/087a6039-e8c1-4afe-8a36-12440c346de1.json": (
        "0444a97480bb40824ae0767eeaea3acb72311bfdebd68bb361d7e5fe1cffe186"
    ),
    "records/246e27d1-827a-4d6f-8a3b-77fc562c94e1.json": (
        "7db0e1059d669eb5a8a707e2973f19b1e0ecb4a6d6c639fdcc4d4ddc2f0eaa46"
    ),
    "records/260108fc-6fe2-4278-bb14-1ba4b0aacaf0.json": (
        "d815e3fa4af460cd1ff582d140e8b176587acd5dabb927289f453753122ee857"
    ),
    "records/36f45589-b18a-4714-b89d-932ac2290f54.json": (
        "08adae82064cc214ba50d0b0130c010d2f521880830f0af7dcf824dfffe68634"
    ),
    "records/51ff4e30-07f3-4c6c-952e-5ca88953c42f.json": (
        "463436d6651fb57a90fa3edb3628821caca00aa83d6901d14a52d884feddc641"
    ),
    "records/6ba14b09-be29-4bc1-add9-520cb0c8ae79.json": (
        "98b32eb5bbd349b815460d1b786a5542e0fe8732f117b42ad5af622bc2fe2250"
    ),
    "records/841d6207-2660-4763-a7da-8439bbca750e.json": (
        "1131f4e262dd71fb8c1e61ce10078524f52980375da9583f74a1a52805ed5f56"
    ),
    "records/90aee5bd-ccef-4f6b-82fb-dbcf228ad30a.json": (
        "f58ede72c972316171061a917e7c69e949f97f505df8e96f443170433fa4b38f"
    ),
    "records/9b387b4e-96f9-4b10-9c65-438fd56a369b.json": (
        "c652966d0dc094b9353e10591f0043111e8c37856a6f312582290474bffae8f7"
    ),
    "records/b82aec70-3efc-4a78-a2c2-0f7c81e79d21.json": (
        "0672bd1736491012580798f9638e03a7ba554b065400085bc75135e66241787c"
    ),
    "records/bc6e90dd-9804-4e39-a0cf-a5094b37b1af.json": (
        "20dbd13b19791bbd72a97cfb55509df908048fb200deb4a2c0acd071316cbfe1"
    ),
    "records/e45b0464-78cc-4208-af27-0633e459f50c.json": (
        "340adb85151caec0598eed2f4ea4f05cf2f85f4572e5c66bbd65a90a25d0e121"
    ),
    "vault.json": "aea83867844843a9a9593ffb9642bccd2538380c26c3e81f7479a653166f3d2f",
}


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> int:
        return self.now

    def stamp(self) -> str:
        return stamp_of(datetime.fromtimestamp(self.now / 1000, UTC))

    def tick(self) -> None:
        self.now += 60_000


type _Device = tuple[
    apsw.Connection,
    Place,
    NoteRepository,
    LabelRepository,
    ColorRepository,
    ReminderRepository,
    ImportRecords,
    About,
]


def _names(mark_id: str, name: str | None) -> str:
    return name or {"todo": "To do"}.get(mark_id, mark_id)


def write_sample(folder: Path) -> dict[str, bytes]:
    clock = _Clock()
    cloud = FakeCloud(DROPBOX)
    pair = recovery.make_recovery_slot(DATABASE_KEY, RECOVERY_KEY_TEXT, "2026-11-01T00:00:00Z")
    file_id, data = key_file(DATA_KEYS, RECOVERY_KEY, pair)
    cloud.put_by_hand(VAULT_FILE, vault_file(VaultInfo(VAULT)))
    cloud.put_by_hand(item_path(KEYS, file_id), data)
    with tempfile.TemporaryDirectory() as work:
        devices: dict[str, _Device] = {}
        for name, device in (("laptop", LAPTOP), ("desk", DESK)):
            db = open_store(Path(work) / f"{name}.db", bytes(32))
            stamp, millis = clock.stamp, clock
            devices[name] = (
                db,
                Place(cloud.target(name), VAULT, DATA_KEYS, device),
                NoteRepository(db, clock=stamp, millis=millis),
                LabelRepository(db, clock=stamp, millis=millis),
                ColorRepository(db, clock=stamp, millis=millis),
                ReminderRepository(db, clock=stamp, device=device, millis=millis),
                ImportRecords(db, clock=stamp, millis=millis),
                About("Laptop · 노트북" if name == "laptop" else "Desk PC", "Linux", "0.7.0"),
            )

        def sync(name: str) -> None:
            db, place, notes, *_, about = devices[name]
            sync_vault(db, place, clock, notes, _names, about)

        db, _, notes, labels, colors, reminders, imports, _ = devices["laptop"]
        work_first = labels.create_category("Work · 업무", "blue", "briefcase")
        labels.rename_mark("todo", "Do · 할 것")
        later = labels.create_mark("Later · 나중에", "flag")
        colors.add("#336699")
        colors.add("#993366")
        text = "A note in format 2\n한글 메모, 그대로 ✓\n⏰2026-12-01 09:00"
        notes.create(text, note_id=NOTE)
        reminders.match_text(NOTE, due(text, NINE).keys())
        notes.set_category(NOTE, work_first.id)
        notes.set_mark(NOTE, later.id, True)
        reminders.set(NOTE, Rule(date(2026, 11, 20), NINE, "Asia/Seoul", "weekly", frozenset({4})))
        notes.create("In the trash · 휴지통", note_id=TRASHED)
        notes.delete(TRASHED)
        notes.create("Emptied · 비움", note_id=EMPTIED)
        notes.create("Edited on both · 둘 다 고침", note_id=EDITED)
        notes.create("Brought in · 가져옴", note_id=BROUGHT)
        imports.record("sticky_notes", "sample-1", "Brought in · 가져옴", BROUGHT)
        clock.tick()
        sync("laptop")

        db, _, notes, labels, colors, reminders, imports, _ = devices["desk"]
        clock.tick()
        # Made later, of the laptop's name: becomes the laptop's.
        work_second = labels.create_category("work · 업무", "green")
        to_do = labels.create_mark("do · 할 것", "square-check")  # the built-in one, renamed
        colors.add("#336699")
        mine = notes.create("Made on the desk · 책상", note_id=None)
        notes.set_category(mine.id, work_second.id)
        notes.set_mark(mine.id, to_do.id, True)
        sync("desk")
        clock.tick()
        notes.update_body(EDITED, "Edited on the desk · 책상에서", keep_earlier=True)
        on_desk = reminders.of_note(NOTE)
        assert on_desk is not None
        clock.tick()
        reminders.snooze(on_desk.id, datetime(2026, 11, 20, 1, tzinfo=UTC))

        db, _, notes, *_ = devices["laptop"]
        clock.tick()
        notes.update_body(EDITED, "Edited on the laptop · 노트북에서", keep_earlier=True)
        notes.delete(EMPTIED)
        notes.purge(EMPTIED)
        for _ in range(3):
            sync("laptop")
            sync("desk")
        for each in devices.values():
            each[0].close()
    files = cloud.files()
    for path, data in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1])
    if target.exists() and any(target.iterdir()):
        sys.exit("the folder is not empty")
    for path, data in sorted(write_sample(target).items()):
        print(f'    "{path}": "{hashlib.sha256(data).hexdigest()}",')
