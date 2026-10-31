$ErrorActionPreference = "Stop"
foreach ($i in 1..4) {
  $file = "tasks/012_vga_aesthetic_h3_5s_batch_{0:d2}.json" -f $i
  Write-Output ("START " + $file)
  uv run python batch_run.py --task $file --endpoints endpoints.json --timeout 2400
  if ($LASTEXITCODE -ne 0) { Write-Error ("FAILED " + $file); exit $LASTEXITCODE }
  Write-Output ("DONE " + $file)
}
