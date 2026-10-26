# Mouse cursor sprite

The cursor is adapted from [Black Cursor.svg by Ort43v](https://commons.wikimedia.org/wiki/File:Black_Cursor.svg),
licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
The adapted SVG and PNG use the same license. Attribution, the changes, and the
license are included in `LICENSE.txt`. `mouse_cursor.json` records the source
URL, author, source hash, rasterization, crop, and hotspot coordinates.

`mouse_cursor.svg` extracts the original cursor path (`path3076`), retains its
black fill and white outline, and removes the shadow and editor metadata. This
is the sharp classic arrow shown in the cursor comparison. The 190×280 SVG uses
view box `(15, 15, 190, 280)` and was rasterized at 570×840 using ImageMagick:

```bash
convert -background none -density 288 mouse_cursor.svg PNG32:mouse_cursor_full.png
```

Cropping to the nonzero alpha bounds `(60, 95, 490, 775)` produces the 430×680
`mouse_cursor.png`. Runtime rendering needs only Pillow and the bundled PNG and
JSON.

The hotspot is `(30.5, 0.5)` in the cropped PNG: the center of the topmost white
tip pixels with alpha at least 128. Coordinates refer to pixel boundaries.
Runtime resizing scales this hotspot along with the sprite and places it within
half an output pixel per axis of the transformed cell center.

`mouse_size` is the cropped sprite's height in final image pixels, with aspect
ratio preserved. The renderer caches resized sprites and alpha-composites them
after the board's noise and geometry, before image encoding.
