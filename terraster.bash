cd "$(dirname "$0")"

# the land tile, 24k cells of bare earth, in 24-bit colour
uv run bin/kingfisher render usgs-64390f6fd34ee8d4ade0b22e --layer elevation_ground

# the same thing as a density ramp, if you want to paste it somewhere
uv run bin/kingfisher render usgs-64390f6fd34ee8d4ade0b22e --layer elevation_ground --ascii

# what the beam actually hit -- canopy and rooftops, not bare earth
uv run bin/kingfisher render usgs-64390f6fd34ee8d4ade0b22e --layer elevation_surface

# return density: where the survey flew, rather than what it found
uv run bin/kingfisher render usgs-64390f6fd34ee8d4ade0b22e --layer point_density

# and the coastal one, mostly ocean -- water_share is the layer that makes it legible
uv run bin/kingfisher render usgs-64390efbd34ee8d4ade0b08a --layer water_share

uv run matters — it needs h3 from the venv. Width defaults to your terminal, so make it wide before you run it; --width N if you want to pin it.

water_share on the coastal tile is the one I'd look at second — it's 98.8% water, so on elevation_surface it renders as almost nothing, but on water_share the coastline draws itself.

And while you're in a fresh shell:

kubectl --context "${KINGFISHER_CONTEXT:-k3d-kingfisher}" -n "${KINGFISHER_NAMESPACE:-kingfisher}" exec deploy/prometheus -- \
  sh -c 'wget -qO- http://127.0.0.1:9090/metrics' | grep otlp/v1/metrics
