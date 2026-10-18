#!/usr/bin/env python3
"""Build a labeled contact sheet of one person's Immich face crops.

Requires: python3 -m pip install requests pillow
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import hashlib
from io import BytesIO
import json
from pathlib import Path
import os
import sys
import threading
from typing import Any

try:
    import requests
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as exc:
    print("Missing dependency. Install them with: python3 -m pip install requests pillow", file=sys.stderr)
    raise SystemExit(2) from exc


THREAD_LOCAL = threading.local()
MAX_IMAGE_DIMENSION = 65500


def session_for_thread(api_key: str) -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        THREAD_LOCAL.session = session
    return session


def request_json(url: str, api_key: str, *, method: str = "GET", **kwargs: Any) -> Any:
    response = session_for_thread(api_key).request(method, url, timeout=90, **kwargs)
    if not response.ok:
        detail = response.text.strip()
        raise requests.HTTPError(
            f"Immich returned HTTP {response.status_code} for {response.url}: {detail}",
            response=response,
        )
    return response.json()


def asset_items(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Return search results and the cursor for the next page, if any."""
    node = payload
    if isinstance(node, dict) and isinstance(node.get("assets"), dict):
        node = node["assets"]
    if isinstance(node, dict):
        items = node.get("items", [])
        return items, node.get("nextCursor")
    if isinstance(node, list):
        return node, None
    return [], None


def fetch_people(api_root: str, api_key: str) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    page = 1
    while True:
        result = request_json(
            f"{api_root}/people", api_key, params={"page": page, "size": 1000}
        )
        node = result.get("people", result) if isinstance(result, dict) else result
        if isinstance(node, dict):
            items = node.get("items", [])
        elif isinstance(node, list):
            items = node
        else:
            items = []
        people.extend(item for item in items if isinstance(item, dict) and item.get("id"))
        if len(items) < 1000:
            break
        page += 1
    return people


def choose_person(people: list[dict[str, Any]], requested_name: str) -> dict[str, Any]:
    query = " ".join(requested_name.casefold().split())
    ranked = []
    for person in people:
        name = " ".join(str(person.get("name") or "").casefold().split())
        if not name:
            continue
        score = SequenceMatcher(None, query, name).ratio()
        if name.startswith(query + " ") or query.startswith(name + " "):
            score = max(score, 0.8)
        if name == query:
            score = 1.0
        if score >= 0.7:
            ranked.append((score, person))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("name", "")).casefold()))
    if not ranked:
        raise RuntimeError(
            f"No Immich person matched '{requested_name}'. Check the spelling or try the person's exact name."
        )
    if len(ranked) == 1:
        return ranked[0][1]

    print(f"Several Immich people are similar to '{requested_name}':", flush=True)
    for index, (score, person) in enumerate(ranked, start=1):
        count = person.get("assetCount", "?")
        print(f"  {index}. {person.get('name') or '(unnamed)'} — {count} assets", flush=True)
    while True:
        answer = input("Enter the number of the correct person (or q to cancel): ").strip().casefold()
        if answer == "q":
            raise RuntimeError("Person selection cancelled.")
        if answer.isdigit() and 1 <= int(answer) <= len(ranked):
            selected = ranked[int(answer) - 1][1]
            print(f"Selected {selected.get('name')}.", flush=True)
            return selected
        print(f"Enter a number from 1 to {len(ranked)}, or q to cancel.", flush=True)


def fetch_assets(api_root: str, person_id: str, api_key: str) -> list[dict[str, Any]]:
    url = f"{api_root}/search/metadata"
    assets: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        body: dict[str, Any] = {
            "size": 1000,
            "withPeople": True,
            "filter": {"personIds": {"all": [person_id]}},
        }
        if cursor:
            body["cursor"] = cursor
        result = request_json(
            url,
            api_key,
            method="POST",
            json=body,
        )
        items, next_cursor = asset_items(result)
        assets.extend(item for item in items if isinstance(item, dict) and item.get("id"))
        print(f"Found {len(assets)} matching assets…", flush=True)
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise RuntimeError("Immich returned a repeated pagination cursor; stopping to avoid a loop.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return assets


def faces_for_asset(api_root: str, asset: dict[str, Any], person_id: str, api_key: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for person in asset.get("people") or []:
        if person.get("id") == person_id:
            matches.extend(face for face in (person.get("faces") or []) if isinstance(face, dict))
    if matches:
        return matches

    # Immich v3 may omit face boxes from search results. Ask for this asset's
    # faces directly in that case and retain only the selected person's boxes.
    payload = request_json(f"{api_root}/faces", api_key, params={"id": asset["id"]})
    if isinstance(payload, dict):
        payload = payload.get("faces", [])
    if not isinstance(payload, list):
        return []
    return [
        face for face in payload
        if isinstance(face, dict)
        and isinstance(face.get("person"), dict)
        and face["person"].get("id") == person_id
    ]


def crop_face(api_root: str, asset: dict[str, Any], face: dict[str, Any], api_key: str) -> Image.Image:
    thumb_url = f"{api_root}/assets/{asset['id']}/thumbnail"
    response = session_for_thread(api_key).get(
        thumb_url, params={"size": "preview", "edited": "true"}, timeout=90
    )
    if response.status_code == 400:
        response = session_for_thread(api_key).get(
            thumb_url, params={"size": "thumbnail", "edited": "true"}, timeout=90
        )
    response.raise_for_status()
    with Image.open(BytesIO(response.content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image_width = int(face.get("imageWidth") or asset.get("width") or image.width)
        image_height = int(face.get("imageHeight") or asset.get("height") or image.height)
        sx, sy = image.width / image_width, image.height / image_height
        left = round(int(face["boundingBoxX1"]) * sx)
        top = round(int(face["boundingBoxY1"]) * sy)
        right = round(int(face["boundingBoxX2"]) * sx)
        bottom = round(int(face["boundingBoxY2"]) * sy)
        left, top = max(0, left), max(0, top)
        right, bottom = min(image.width, right), min(image.height, bottom)
        if right <= left or bottom <= top:
            raise ValueError("Immich returned an empty face rectangle")
        return image.crop((left, top, right, bottom)).copy()


def crop_cache_path(
    cache_directory: Path, api_root: str, person_id: str,
    asset: dict[str, Any], face: dict[str, Any],
) -> Path:
    identity = {
        "server": api_root.rstrip("/"),
        "person_id": person_id,
        "asset_id": asset.get("id"),
        "asset_checksum": asset.get("checksum"),
        "asset_updated_at": asset.get("updatedAt") or asset.get("fileModifiedAt"),
        "face_id": face.get("id"),
        "face_box": [
            face.get("boundingBoxX1"), face.get("boundingBoxY1"),
            face.get("boundingBoxX2"), face.get("boundingBoxY2"),
            face.get("imageWidth"), face.get("imageHeight"),
        ],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return cache_directory / f"{digest}.jpg"


def cached_or_fetch_crop(
    api_root: str, person_id: str, asset: dict[str, Any], face: dict[str, Any],
    api_key: str, cache_directory: Path, force_fetch: bool,
) -> tuple[Image.Image, bool]:
    """Load a cached face crop or fetch it and update the local cache."""
    cache_path = crop_cache_path(cache_directory, api_root, person_id, asset, face)
    if cache_path.is_file() and not force_fetch:
        try:
            with Image.open(cache_path) as cached:
                return ImageOps.exif_transpose(cached).convert("RGB").copy(), True
        except Exception:
            # A truncated or unsupported cache entry is refreshed from Immich.
            pass

    crop = crop_face(api_root, asset, face, api_key)
    cache_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_name(f"{cache_path.stem}.{threading.get_ident()}.tmp.jpg")
    crop.save(temporary_path, format="JPEG", quality=95, optimize=True)
    temporary_path.replace(cache_path)
    return crop, False


def label_for(asset: dict[str, Any], index: int, include_metadata: bool) -> str:
    if not include_metadata:
        return ""
    date = str(asset.get("localDateTime") or asset.get("fileCreatedAt") or "")[:10]
    name = str(asset.get("originalFileName") or asset.get("id") or "")
    return f"{index:04d} {date}\n{name}"


def sheet_dimensions(
    face_count: int, columns: int, tile: int, spacing: int, include_metadata: bool
) -> tuple[int, int]:
    label_height = 30 if include_metadata else 0
    cell_height = tile + label_height
    rows = (face_count + columns - 1) // columns
    width = columns * tile + max(0, columns - 1) * spacing
    height = rows * cell_height + max(0, rows - 1) * spacing
    return width, height


def sheet_dimension_suggestions(
    face_count: int, columns: int, tile: int, spacing: int, include_metadata: bool
) -> list[str]:
    """Suggest settings that keep the contact sheet within Pillow's dimension limit."""
    suggestions = []
    label_height = 30 if include_metadata else 0
    cell_height = tile + label_height
    max_rows = (MAX_IMAGE_DIMENSION + spacing) // (cell_height + spacing)

    # More columns preserves every face at its current size, as long as the
    # resulting width also remains within Pillow's limit.
    if max_rows > 0:
        needed_columns = max(columns, (face_count + max_rows - 1) // max_rows)
        width, height = sheet_dimensions(
            face_count, needed_columns, tile, spacing, include_metadata
        )
        if needed_columns > columns and width <= MAX_IMAGE_DIMENSION:
            suggestions.append(
                f"Recommended: --faces-per-row {needed_columns} (keeps all {face_count:,} face crops "
                f"at {tile}px; sheet {width:,} x {height:,})"
            )

    # Keep all faces and the current column count, using the largest smaller
    # tile that fits. Respect the CLI's minimum supported tile size.
    for smaller_tile in range(tile - 1, 31, -1):
        width, height = sheet_dimensions(
            face_count, columns, smaller_tile, spacing, include_metadata
        )
        if width <= MAX_IMAGE_DIMENSION and height <= MAX_IMAGE_DIMENSION:
            suggestions.append(
                f"--face-size {smaller_tile} (keeps all {face_count:,} face crops and "
                f"{columns} faces per row; sheet {width:,} x {height:,})"
            )
            break

    # Keep the current tile and column count, and cap the number of faces to
    # the largest complete set that fits.
    if max_rows > 0:
        max_faces = columns * max_rows
        if max_faces < face_count and max_faces > 0:
            width, height = sheet_dimensions(
                max_faces, columns, tile, spacing, include_metadata
            )
            suggestions.append(
                f"--max-faces {max_faces} (keeps {columns} faces per row at {tile}px; "
                f"sheet {width:,} x {height:,})"
            )
    return suggestions


def validate_sheet_dimensions(
    face_count: int, columns: int, tile: int, spacing: int, include_metadata: bool
) -> None:
    width, height = sheet_dimensions(face_count, columns, tile, spacing, include_metadata)
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        message = (
            f"The contact sheet would be {width} x {height} pixels, exceeding Pillow's "
            f"{MAX_IMAGE_DIMENSION}-pixel limit per dimension. Reduce --max-faces or "
            "--face-size, or increase --faces-per-row."
        )
        suggestions = sheet_dimension_suggestions(
            face_count, columns, tile, spacing, include_metadata
        )
        if suggestions:
            message += (
                f"\nFor {face_count:,} face crops with the current settings "
                f"(--faces-per-row {columns}, --face-size {tile}, "
                f"--face-spacing {spacing}), try one of:"
            )
            for suggestion in suggestions:
                message += f"\n  {suggestion}"
        raise RuntimeError(message)


def build_sheet(
    crops: list[tuple[Image.Image, str]], output: Path, columns: int, tile: int,
    spacing: int, include_metadata: bool,
) -> None:
    if not crops:
        raise RuntimeError("No face crops were found for that person.")
    validate_sheet_dimensions(len(crops), columns, tile, spacing, include_metadata)
    label_height = 30 if include_metadata else 0
    cell_height = tile + label_height
    rows = (len(crops) + columns - 1) // columns
    width, height = sheet_dimensions(len(crops), columns, tile, spacing, include_metadata)
    sheet = Image.new("RGB", (width, height), "#202124")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 10)
    except OSError:
        font = ImageFont.load_default()
    for index, (crop, label) in enumerate(crops):
        x = (index % columns) * (tile + spacing)
        y = (index // columns) * (cell_height + spacing)
        fitted = ImageOps.contain(crop, (tile - 8, tile - 8), Image.Resampling.LANCZOS)
        sheet.paste(fitted, (x + (tile - fitted.width) // 2, y + (tile - fitted.height) // 2))
        if include_metadata:
            draw.multiline_text((x + 2, y + tile), label[:90], fill="white", font=font, spacing=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92, optimize=True)


def build_collage(
    crops: list[tuple[Image.Image, str]], output: Path, columns: int,
    face_size: int, spacing: int,
) -> None:
    """Make a compact masonry-style collage, preserving each crop's aspect ratio."""
    if not crops:
        raise RuntimeError("No face crops were found for the collage.")

    scaled = [
        ImageOps.contain(crop, (face_size, face_size), Image.Resampling.LANCZOS)
        for crop, _label in crops
    ]
    rows = [scaled[i:i + columns] for i in range(0, len(scaled), columns)]
    row_widths = [sum(image.width for image in row) + spacing * (len(row) - 1) for row in rows]
    row_heights = [max(image.height for image in row) for row in rows]
    width = max(row_widths)
    height = sum(row_heights) + spacing * (len(rows) - 1)
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise RuntimeError(
            f"The collage would be {width} x {height} pixels, exceeding Pillow's "
            f"{MAX_IMAGE_DIMENSION}-pixel limit per dimension. Reduce --max-faces or "
            "--face-size, or increase --faces-per-row."
        )

    collage = Image.new("RGB", (width, height), "#202124")
    y = 0
    for row, row_width, row_height in zip(rows, row_widths, row_heights):
        x = (width - row_width) // 2
        for image in row:
            collage.paste(image, (x, y + (row_height - image.height) // 2))
            x += image.width + spacing
        y += row_height + spacing
    output.parent.mkdir(parents=True, exist_ok=True)
    collage.save(output, quality=92, optimize=True)


def photomosaic_grid(source: Image.Image, face_size: int) -> tuple[int, int]:
    columns = max(1, round(source.width / face_size))
    rows = max(1, round(source.height / face_size))
    return columns, rows


def minimum_photomosaic_tile_size(source: Image.Image, face_count: int) -> int:
    """Find the smallest tile size whose grid does not exceed the available crops."""
    low, high = 1, max(source.width, source.height)
    while low < high:
        candidate = (low + high) // 2
        columns, rows = photomosaic_grid(source, candidate)
        if columns * rows <= face_count:
            high = candidate
        else:
            low = candidate + 1
    return low


def choose_photomosaic_tile_size(
    source: Image.Image, face_count: int, requested_size: int | None
) -> int | None:
    minimum_size = minimum_photomosaic_tile_size(source, face_count)
    columns, rows = photomosaic_grid(source, minimum_size)
    print(
        f"Found {face_count:,} face crops. The smallest tile size that keeps the "
        f"photomosaic grid within that count is {minimum_size}px "
        f"({columns * rows:,} tiles). Smaller tiles create more tiles than available "
        "face crops, so some faces may need to repeat.",
        flush=True,
    )
    if requested_size is not None:
        return requested_size

    while True:
        try:
            answer = input(
                f"Photomosaic tile size in pixels [{minimum_size}] (or q to cancel): "
            ).strip()
        except EOFError:
            raise RuntimeError(
                "No interactive input is available; pass --photomosaic-tile-size PIXELS."
            )
        if answer.casefold() == "q":
            return None
        if not answer:
            return minimum_size
        try:
            size = int(answer)
        except ValueError:
            print("Enter a whole number of pixels, or q to cancel.")
            continue
        if size < 1:
            print("Tile size must be at least 1 pixel.")
            continue
        if size < minimum_size:
            print(
                f"That size creates more than {face_count:,} tiles, so some face crops "
                "will be reused."
            )
        return size


def validate_photomosaic_dimensions(source: Image.Image, face_size: int, spacing: int) -> tuple[int, int]:
    columns, rows = photomosaic_grid(source, face_size)
    width = columns * face_size + max(0, columns - 1) * spacing
    height = rows * face_size + max(0, rows - 1) * spacing
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise RuntimeError(
            f"The photomosaic would be {width} x {height} pixels, exceeding Pillow's "
            f"{MAX_IMAGE_DIMENSION}-pixel limit per dimension. Use a smaller input image, "
            "reduce --photomosaic-tile-size, or reduce --face-spacing."
        )
    return width, height


def build_photomosaic(
    crops: list[tuple[Image.Image, str]], source: Image.Image, output: Path,
    face_size: int, spacing: int,
) -> None:
    """Fill a target image with face tiles selected by average color."""
    if not crops:
        raise RuntimeError("No face crops were found for the photomosaic.")
    columns, rows = photomosaic_grid(source, face_size)
    width, height = validate_photomosaic_dimensions(source, face_size, spacing)
    target_colors = source.resize((columns, rows), Image.Resampling.LANCZOS).convert("RGB")
    target_pixels = list(target_colors.get_flattened_data())

    tiles: list[Image.Image] = []
    colors: list[tuple[int, int, int]] = []
    for crop, _label in crops:
        tile = ImageOps.fit(crop.convert("RGB"), (face_size, face_size), Image.Resampling.LANCZOS)
        tiles.append(tile)
        colors.append(tile.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0)))

    mosaic = Image.new("RGB", (width, height), "#202124")
    usage = [0] * len(tiles)
    unused = set(range(len(tiles))) if len(target_pixels) <= len(tiles) else set()
    previous_row: list[int] = []
    for index, target_color in enumerate(target_pixels):
        x_index, y_index = index % columns, index // columns
        best_candidate = 0
        best_score: int | None = None
        left_candidate = previous_row[x_index - 1] if x_index > 0 else None
        candidates = unused if unused else range(len(colors))
        for candidate in candidates:
            color = colors[candidate]
            red = target_color[0] - color[0]
            green = target_color[1] - color[1]
            blue = target_color[2] - color[2]
            score = red * red + green * green + blue * blue + usage[candidate] * 500
            if candidate == left_candidate and len(tiles) > 1:
                score += 100000
            if best_score is None or score < best_score:
                best_candidate, best_score = candidate, score

        x = x_index * (face_size + spacing)
        y = y_index * (face_size + spacing)
        mosaic.paste(tiles[best_candidate], (x, y))
        usage[best_candidate] += 1
        unused.discard(best_candidate)
        if x_index == 0:
            previous_row = []
        previous_row.append(best_candidate)

    output.parent.mkdir(parents=True, exist_ok=True)
    mosaic.save(output, quality=92, optimize=True)


def save_crops(crops: list[tuple[Image.Image, str]], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, (crop, _label) in enumerate(crops, start=1):
        path = directory / f"face_{index:04d}.jpg"
        suffix = 1
        while path.exists():
            suffix += 1
            path = directory / f"face_{index:04d}_{suffix}.jpg"
        crop.save(path, format="JPEG", quality=95, optimize=True)


def confirm_output_path(requested: str) -> Path | None:
    output = Path(requested).expanduser()
    while output.exists():
        if output.is_dir():
            print(f"The output path is a directory: {output}", file=sys.stderr)
            return None
        answer = input(f"{output} already exists. Overwrite it? [y/N]: ").strip().casefold()
        if answer in {"y", "yes"}:
            return output
        if answer in {"n", "no", ""}:
            while True:
                new_name = input("Enter a new output filename (or q to quit): ").strip()
                if new_name.casefold() == "q":
                    return None
                if not new_name:
                    print("Please enter a filename, or q to quit.")
                    continue
                output = Path(new_name).expanduser()
                if output.exists() and output.is_dir():
                    print(f"That path is a directory: {output}")
                    continue
                break
            continue
        print("Please answer y or n.")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "API key: pass --api-key or set IMMICH_API_KEY. If you do not have a key, "
            "create one in Immich Account Settings > API Keys with read access to people and assets. "
            "You can also run this program with the 'help' command."
        ),
    )
    parser.add_argument("--base-url", required=True, help="Immich URL, such as http://192.168.1.200:2283")
    parser.add_argument("--person-name", required=True, help="Person name as shown in Immich")
    parser.add_argument("--api-key", help="Immich API key (or set IMMICH_API_KEY)")
    parser.add_argument(
        "--mode", choices=("contact-sheet", "collage", "photomosaic"),
        default="contact-sheet",
        help="Output type (default: contact-sheet)",
    )
    parser.add_argument("--output", help="Output JPEG path (default depends on --mode)")
    parser.add_argument(
        "--faces-per-row", "--columns", dest="columns", type=int, default=10,
        help="Number of face crops per row (default: 10; --columns is an alias)",
    )
    parser.add_argument(
        "--face-size", "--tile-size", dest="tile_size", type=int, default=112,
        help="Face tile width and height in pixels (default: 112; --tile-size is an alias)",
    )
    parser.add_argument(
        "--photomosaic-tile-size", type=int, metavar="PIXELS",
        help="Photomosaic tile size; if omitted, calculate a minimum and prompt for a choice",
    )
    parser.add_argument(
        "--include-metadata", action="store_true",
        help="Show source filename and date below each face (metadata is omitted by default)",
    )
    parser.add_argument(
        "--face-spacing", type=int, default=0,
        help="Pixel gap between face tiles (default: 0)",
    )
    parser.add_argument(
        "--save-crops", action="store_true",
        help="Save individual face crops in the local _crops/ folder (off by default)",
    )
    parser.add_argument(
        "--force-fetch", action="store_true",
        help="Fetch face previews again instead of reusing cached crops, then refresh the cache",
    )
    parser.add_argument(
        "--photomosaic-image", metavar="PATH",
        help="Target image required when --mode photomosaic is selected",
    )
    parser.add_argument(
        "--max-faces", type=int, default=None,
        help="Maximum face crops to include; by default include all faces found",
    )
    parser.add_argument("--workers", type=int, default=6, help="Concurrent Immich requests (default: 6)")
    if sys.argv[1:] == ["help"]:
        parser.print_help()
        return 0
    args = parser.parse_args()
    if args.columns < 1 or args.tile_size < 32 or args.workers < 1:
        parser.error("faces-per-row and workers must be positive; face-size must be at least 32")
    if args.face_spacing < 0:
        parser.error("face-spacing cannot be negative")
    if args.max_faces is not None and args.max_faces < 1:
        parser.error("max-faces must be a positive integer")
    if args.mode == "photomosaic" and not args.photomosaic_image:
        parser.error("--photomosaic-image is required when --mode photomosaic is selected")
    if args.mode != "photomosaic" and args.photomosaic_image:
        parser.error("--photomosaic-image can only be used with --mode photomosaic")
    if args.mode != "photomosaic" and args.photomosaic_tile_size is not None:
        parser.error("--photomosaic-tile-size can only be used with --mode photomosaic")
    if args.photomosaic_tile_size is not None and args.photomosaic_tile_size < 1:
        parser.error("photomosaic-tile-size must be at least 1 pixel")
    if args.mode != "contact-sheet" and args.include_metadata:
        parser.error("--include-metadata can only be used with --mode contact-sheet")

    photomosaic_source = None
    if args.photomosaic_image:
        source_path = Path(args.photomosaic_image).expanduser()
        if not source_path.is_file():
            parser.error(f"photomosaic input image does not exist: {source_path}")
        try:
            with Image.open(source_path) as source_file:
                photomosaic_source = ImageOps.exif_transpose(source_file).convert("RGB")
        except Exception as exc:
            parser.error(f"could not open photomosaic input image {source_path}: {exc}")
        if args.photomosaic_tile_size is not None:
            try:
                validate_photomosaic_dimensions(
                    photomosaic_source, args.photomosaic_tile_size, args.face_spacing
                )
            except RuntimeError as exc:
                parser.error(str(exc))
    default_outputs = {
        "contact-sheet": "face-sheet.jpg",
        "collage": "face-collage.jpg",
        "photomosaic": "face-photomosaic.jpg",
    }
    output = confirm_output_path(args.output or default_outputs[args.mode])
    if output is None:
        print("Output cancelled; no file was written.")
        return 1
    if photomosaic_source is not None and output.resolve() == Path(args.photomosaic_image).expanduser().resolve():
        parser.error("photomosaic output path would overwrite its input image")

    base_url = args.base_url.rstrip("/")
    api_root = f"{base_url}/api"
    api_key = args.api_key or os.environ.get("IMMICH_API_KEY")
    if not api_key:
        print(
            "No Immich API key was provided. Create one in Immich > Account Settings > API Keys "
            "with read access to people and assets, then pass it with --api-key or set IMMICH_API_KEY.",
            file=sys.stderr,
        )
        return 2

    print("Run settings:", flush=True)
    print(f"  Output mode: {args.mode}", flush=True)
    print(f"  Immich server: {base_url}", flush=True)
    print(f"  Person name: {args.person_name}", flush=True)
    print(f"  Output file: {output}", flush=True)
    if args.mode == "photomosaic":
        tile_setting = (
            f"{args.photomosaic_tile_size} pixels"
            if args.photomosaic_tile_size is not None else "choose after finding face crops"
        )
        print(f"  Photomosaic tile size: {tile_setting}", flush=True)
    else:
        print(f"  Faces per row: {args.columns}", flush=True)
        print(f"  Face size: {args.tile_size} x {args.tile_size} pixels", flush=True)
    print(f"  Space between faces: {args.face_spacing} pixels", flush=True)
    print(f"  Maximum faces: {args.max_faces if args.max_faces is not None else 'all'}", flush=True)
    print(
        "  Crop cache: "
        + ("refresh from Immich" if args.force_fetch else "reuse local crops when available"),
        flush=True,
    )
    if args.mode == "contact-sheet":
        print(f"  Metadata: {'included' if args.include_metadata else 'omitted'}", flush=True)
    print(f"  Save individual crops: {'yes (_crops/)' if args.save_crops else 'no'}", flush=True)
    if photomosaic_source is not None:
        print(f"  Photomosaic target: {args.photomosaic_image}", flush=True)
    print(f"  Concurrent requests: {args.workers}", flush=True)
    print("  API key: provided (value hidden)", flush=True)

    try:
        people = fetch_people(api_root, api_key)
        person = choose_person(people, args.person_name)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 401 or status == 403:
            print("Immich rejected the key. Create or update a key with read access to people and assets.", file=sys.stderr)
        else:
            print(f"Could not list Immich people ({status}): {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, EOFError) as exc:
        print(exc, file=sys.stderr)
        return 1

    person_id = person["id"]
    person_name = person.get("name") or args.person_name

    try:
        print(f"Getting assets for {person_name}…", flush=True)
        assets = fetch_assets(api_root, person_id, api_key)
        if not assets:
            raise RuntimeError("Immich returned no matching assets. Check the person ID and API key permissions.")

        if args.mode == "contact-sheet":
            estimated_faces = min(args.max_faces, len(assets)) if args.max_faces is not None else len(assets)
            validate_sheet_dimensions(
                estimated_faces, args.columns, args.tile_size,
                args.face_spacing, args.include_metadata,
            )
        crops: list[tuple[Image.Image, str]] = []
        cache_hits = 0
        errors = 0
        cache_directory = Path.cwd() / "_crops" / "cache"

        def process(asset: dict[str, Any]) -> list[tuple[Image.Image, bool, str]]:
            faces = faces_for_asset(api_root, asset, person_id, api_key)
            return [
                (
                    *cached_or_fetch_crop(
                        api_root, person_id, asset, face, api_key,
                        cache_directory, args.force_fetch,
                    ),
                    label_for(asset, len(faces), args.include_metadata),
                )
                for face in faces
            ]

        processed = 0
        batch_size = args.workers
        for start in range(0, len(assets), batch_size):
            batch = assets[start:start + batch_size]
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(process, asset) for asset in batch]
                # Collect each batch in Immich's result order so a maximum
                # always selects the same first faces when rerun.
                for asset, future in zip(batch, futures):
                    try:
                        result = future.result()
                        for crop, was_cached, _label in result:
                            crops.append((crop, label_for(
                                asset, len(crops) + 1, args.include_metadata
                            )))
                            cache_hits += int(was_cached)
                            if args.max_faces is not None and len(crops) >= args.max_faces:
                                break
                    except Exception as exc:
                        errors += 1
                        print(f"Skipped {asset.get('originalFileName') or asset['id']}: {exc}", file=sys.stderr)
                    processed += 1
                    if processed % 100 == 0 or processed == len(assets) or (
                        args.max_faces is not None and len(crops) >= args.max_faces
                    ):
                        print(f"Processed {processed}/{len(assets)} assets; {len(crops)} face crops", flush=True)
                    if args.max_faces is not None and len(crops) >= args.max_faces:
                        break
            if args.max_faces is not None and len(crops) >= args.max_faces:
                break

        print(
            f"Reused {cache_hits} cached face crops; fetched {len(crops) - cache_hits} new previews.",
            flush=True,
        )

        photomosaic_tile_size = None
        if args.mode == "photomosaic":
            if not crops:
                raise RuntimeError("No face crops were found for the photomosaic.")
            photomosaic_tile_size = choose_photomosaic_tile_size(
                photomosaic_source, len(crops), args.photomosaic_tile_size
            )
            if photomosaic_tile_size is None:
                print("Photomosaic cancelled; no output was written.")
                return 1
            validate_photomosaic_dimensions(
                photomosaic_source, photomosaic_tile_size, args.face_spacing
            )

        if args.save_crops:
            crop_directory = Path.cwd() / "_crops"
            save_crops(crops, crop_directory)
            print(f"Saved individual face crops to {crop_directory}", flush=True)

        if args.mode == "contact-sheet":
            build_sheet(
                crops, output, args.columns, args.tile_size,
                args.face_spacing, args.include_metadata,
            )
        elif args.mode == "collage":
            build_collage(crops, output, args.columns, args.tile_size, args.face_spacing)
        else:
            build_photomosaic(
                crops, photomosaic_source, output,
                photomosaic_tile_size, args.face_spacing,
            )
        print(f"Saved {len(crops)} face crops for {person_name} to {output.resolve()}")
        if errors:
            print(f"Skipped {errors} assets because Immich returned an error for them.", file=sys.stderr)
        return 0
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 401 or status == 403:
            print("Immich rejected the key. Create an API key with asset read access and try again.", file=sys.stderr)
        else:
            print(f"Immich request failed ({status}): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Could not create the {args.mode}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
