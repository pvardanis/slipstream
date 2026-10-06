"""The orchestration layer: Prefect-driven sweep resume, kept off the bench-client.

ADR-0012 §Amendment: Prefect and ``prefect_aws`` ship only in the ``orchestration``
extra, and every Prefect import here is lazy (inside functions), so importing this
package pulls no Prefect at module load and no ``sweep`` import path can reach it.
Dependencies flow inward: this layer may import core bench-execution, never the reverse.
"""
