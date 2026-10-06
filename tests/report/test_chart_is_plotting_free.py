"""Pin the chart split: the table renderers import without the plotting stack.

``report.chart`` holds the pure Markdown/JSON table renderers; the matplotlib/seaborn
plotters live in ``report.plotters``. A caller that only renders tables must not drag in
matplotlib, so this loads ``report.chart`` in a clean interpreter and asserts the plotting
stack stayed out of ``sys.modules`` (ADR-0017). The import runs in a subprocess because an
in-process check would see matplotlib already imported by the plotter tests.
"""

import subprocess
import sys


def test_rendering_tables_does_not_import_the_plotting_stack() -> None:
    """Importing the table renderers leaves matplotlib, pandas, and seaborn unloaded."""
    probe = (
        "import slipstream_bench.report.chart as chart\n"
        "import sys\n"
        "assert chart.rows_to_markdown is not None\n"
        "leaked = sorted(\n"
        "    name\n"
        "    for name in ('matplotlib', 'pandas', 'seaborn')\n"
        "    if name in sys.modules\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
