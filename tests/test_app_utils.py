import os

from utils.app_utils import duplicate_uploaded_files


def test_uploaded_files_are_copied_and_other_values_kept(tmp_path, monkeypatch):
    saved = tmp_path / "static" / "images" / "saved"
    saved.mkdir(parents=True)
    (saved / "photo.png").write_bytes(b"png")
    (saved / "other.jpg").write_bytes(b"jpg")
    elsewhere = tmp_path / "elsewhere.png"
    elsewhere.write_bytes(b"x")
    monkeypatch.setenv("SRC_DIR", str(tmp_path))

    settings = {"imageFiles[]": [str(saved / "photo.png"), str(saved / "other.jpg")],
                "single": str(saved / "photo.png"),
                "outside": str(elsewhere),
                "city": "Brussels", "count": 3, "missing": str(saved / "nope.png")}
    result = duplicate_uploaded_files(settings)

    assert result["city"] == "Brussels" and result["count"] == 3
    assert result["outside"] == str(elsewhere)              # not an upload, untouched
    assert result["missing"] == str(saved / "nope.png")     # not on disk, untouched
    assert result["imageFiles[]"] == [str(saved / "photo_copy1.png"), str(saved / "other_copy1.jpg")]
    assert result["single"] == str(saved / "photo_copy2.png")   # second copy of the same file gets a new name
    for path in result["imageFiles[]"] + [result["single"]]:
        assert os.path.isfile(path)
    assert (saved / "photo.png").read_bytes() == b"png"      # original untouched
    assert settings["imageFiles[]"][0].endswith("photo.png")  # input not mutated


def test_empty_settings():
    assert duplicate_uploaded_files(None) == {}
    assert duplicate_uploaded_files({}) == {}
