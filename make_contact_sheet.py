import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


root = Path("poster-worker/cache/previews")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
cell_width, cell_height = 280, 420
columns = 4
rows = (len(manifest) + columns - 1) // columns
sheet = Image.new("RGB", (columns * cell_width, rows * (cell_height + 30)), (32, 32, 32))
draw = ImageDraw.Draw(sheet)
font = ImageFont.load_default()

for index, entry in enumerate(manifest):
    image = Image.open(entry["file"]).convert("RGB")
    image.thumbnail((cell_width - 20, cell_height - 20))
    left = (index % columns) * cell_width + (cell_width - image.width) // 2
    top = (index // columns) * (cell_height + 30) + (cell_height - image.height) // 2
    sheet.paste(image, (left, top))
    draw.text(((index % columns) * cell_width + 8, (index // columns) * (cell_height + 30) + cell_height + 5), f"{entry['category']} {entry['index']}  {entry['id']}", fill="white", font=font)

sheet.save(root / "contact-sheet.jpg", quality=92)
