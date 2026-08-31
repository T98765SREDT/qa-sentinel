# Local benchmark evidence

The benchmark is a repeatable fixture for comparing changes on the same
machine. It never contacts an external service.

Run it from the repository root:

```bash
python3 scripts/benchmark.py --requests 100 --repetitions 3 --output benchmark.json
```

It measures:

- 100 independent `/health` requests per repetition at workers `1`, `4`, and
  `16`;
- a six-step, multi-layer schema-v2 dependency DAG;
- JSON, HTML, and JUnit report generation;
- two synthetic report artifacts summarized through `qa-sentinel trend`.

The JSON output records Python, operating system, machine, processor, fixture
size, every timing sample, median and p95 values, workflow duration, and an
explicit `external_traffic: false` marker. Re-run it after meaningful changes
and compare files from the same machine; network, CPU load, Python version, and
the operating system all affect the numbers.

This is deliberately not a load test. It does not measure throughput limits,
durability, browser behavior, or production performance, and the results must
not be presented as those claims.
