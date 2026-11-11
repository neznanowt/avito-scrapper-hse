import io

from PIL import Image

from realty_scraper.photos import hamming, phash, process_image

from .fakeavito import jpeg


def test_process_resizes_and_hashes():
    data = jpeg(7, size=(1280, 960))
    out, info = process_image(data, 384, 85)
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 384 and img.size == (384, 288)
    assert (info["orig_width"], info["orig_height"]) == (1280, 960)
    assert len(info["phash"]) == 16 and len(info["sha1"]) == 40


def test_phash_matches_near_duplicates_only():
    a = Image.open(io.BytesIO(jpeg(7, size=(1280, 960))))
    near = a.resize((640, 480))  # тот же снимок другого размера
    buf = io.BytesIO()
    near.save(buf, "JPEG", quality=40)  # и с сильным сжатием
    near = Image.open(io.BytesIO(buf.getvalue()))
    other = Image.open(io.BytesIO(jpeg(8, size=(1280, 960))))
    assert hamming(phash(a), phash(near)) <= 6
    assert hamming(phash(a), phash(other)) > 10


def test_phash_compatible_with_imagehash_if_installed():
    try:
        import imagehash
    except ImportError:
        return
    img = Image.open(io.BytesIO(jpeg(3)))
    assert hamming(phash(img), str(imagehash.phash(img))) <= 2
