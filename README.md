# Immich Face Sheet

This script finds an Immich person by name, retrieves the assets associated with that person, and crops the face rectangles Immich has identified. It arranges the crops in a JPEG contact sheet. Source metadata is omitted by default and can be added with an option. It reads from Immich and does not change or upload any assets.

If several people have similar names, the script lists the matches and asks you to choose one. Face crops are requested from Immich's preview images, so the resulting faces may be lower resolution than the original photos.

## Requirements

- Python 3.10 or newer
- Network access to your Immich server
- An Immich API key with read access to the relevant people and assets
- The `requests` and `pillow` Python packages

Install the packages with:

```sh
python3 -m pip install requests pillow
```

## Get an Immich API key

In Immich, open your user menu, go to **Account Settings**, and open **API Keys**. Create a key that can read people and assets. If the script reports `401` or `403`, check that the key is valid and has those read permissions. See Immich's [user settings guide](https://docs.immich.app/features/user-settings/) for API key management.

The script accepts a key through the `IMMICH_API_KEY` environment variable or the `--api-key` option. The environment variable is recommended because command-line arguments can be saved in shell history.

In zsh, enter the key without displaying it or saving it in shell history:

```zsh
read -s "IMMICH_API_KEY?Paste your Immich API key: "
echo
export IMMICH_API_KEY
```

Alternatively, pass `--api-key YOUR_KEY` to the script.

## Run it

From this directory, run:

```sh
python3 immich_face_sheet.py \
  --base-url "http://192.168.1.200:2283" \
  --person-name "Joana" \
  --output "joana_faces.jpg"
```

The output path is relative to the directory where you run the command, unless you provide an absolute path.
If that file already exists, the script asks whether to overwrite it. If you decline, it asks for a different filename; enter `q` to cancel.
Before making requests to Immich, the script prints the effective run settings, including faces per row, face size, spacing, maximum faces, and whether metadata is included. It never prints the API key.
Pass `--save-crops` to also save each face included in the contact sheet as a separate JPEG in the `_crops/` folder under your current working directory. This is off by default. Existing crop files are kept; new files receive a numeric suffix if a filename is already in use.
Pass `--create-collage` to produce an additional compact, masonry-style image from the same crops. It preserves each crop's proportions and uses the configured face size, faces per row, and spacing. The collage is saved beside the contact sheet with `_collage` added to its filename (for example, `joana_faces_collage.jpg`). This option is off by default and uses the existing Pillow dependency.
The script checks the estimated sheet dimensions before downloading face previews and checks the exact dimensions before writing. If a sheet would exceed Pillow's 65,500-pixel limit, reduce `--max-faces` or `--face-size`, or increase `--faces-per-row`.

To list all options, run either:

```sh
python3 immich_face_sheet.py --help
python3 immich_face_sheet.py help
```

## Command-line options

| Option | Description | Default |
| --- | --- | --- |
| `--base-url URL` | Immich server URL, without `/api`. | Required |
| `--person-name NAME` | Person name to look up in Immich. Similar matches trigger a selection prompt. | Required |
| `--api-key KEY` | Immich API key. `IMMICH_API_KEY` is used when this option is omitted. | Environment variable |
| `--output PATH` | Output JPEG contact sheet. | `face-sheet.jpg` |
| `--faces-per-row N` | Number of face crops on each row. `--columns` is an alias. | `10` |
| `--face-size PIXELS` | Width and height of each square face tile. `--tile-size` is an alias. | `112` |
| `--include-metadata` | Show source filename and date below each face. | Off |
| `--face-spacing N` | Pixel gap between face tiles. | `0` |
| `--save-crops` | Save each included face as a separate JPEG in `_crops/`. | Off |
| `--create-collage` | Also create a compact collage beside the contact sheet. | Off |
| `--max-faces N` | Maximum number of face crops to include, taken in Immich's search order. If omitted, include all matching faces. | All |
| `--workers N` | Number of concurrent requests to Immich. | `6` |
| `-h`, `--help`, `help` | Show usage information. | — |

For example, make wider face tiles, show 24 faces per row, limit the output to 300 faces, add an 8-pixel gap, and include metadata:

```sh
python3 immich_face_sheet.py \
  --base-url "http://192.168.1.200:2283" \
  --person-name "Joana" \
  --output "joana_faces.jpg" \
  --faces-per-row 24 \
  --face-size 180 \
  --max-faces 300 \
  --face-spacing 8 \
  --include-metadata \
  --create-collage
```
