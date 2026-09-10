"""Small, deterministic gzip/LZMA workload; runs unchanged inside each worker."""

import gzip
import hashlib
import json
import lzma
import statistics
import sys
import time
from pathlib import Path


def benchmark(algorithm: str) -> dict[str, object]:
    source = Path("logs.jsonl").read_bytes()
    compress = gzip.compress if algorithm == "gzip" else lzma.compress
    decompress = gzip.decompress if algorithm == "gzip" else lzma.decompress
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        packed = compress(source)
        timings.append(time.perf_counter() - start)
        assert decompress(packed) == source
    Path("archive.bin").write_bytes(packed)
    result = {
        "algorithm": algorithm,
        "input_bytes": len(source),
        "archive_bytes": len(packed),
        "ratio": len(packed) / len(source),
        "compression_seconds": timings,
        "median_seconds": statistics.median(timings),
        "input_sha256": hashlib.sha256(source).hexdigest(),
        "archive_sha256": hashlib.sha256(packed).hexdigest(),
        "roundtrip_verified": True,
    }
    Path("metrics.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(benchmark(sys.argv[1])))
