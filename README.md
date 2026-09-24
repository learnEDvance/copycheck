<h1 align="center"><span style="color:#ff0044">C</span><span style="color:#ff6a00">o</span><span style="color:#ffb300">p</span><span style="color:#00cc66">y</span><span style="color:#00aaff">C</span><span style="color:#7a5cff">h</span><span style="color:#d500f9">e</span><span style="color:#ff0044">c</span><span style="color:#ff6a00">k</span></h1>

**Aim:** Measure how accurately local LLMs reproduce input text verbatim.

**Why:** Verbatim copy fidelity is the tightest probe of a model's raw reliability for text-editing tasks.

## Live Status

<pre>
┌─CopyCheck live run · refresh: every cell commit (~72 pushes / run)───┐
├────────────────────────────┬─────────────────────────────────────────┤
│ loaded model               │ no model yet                            │
│ samples done               │ 0 / 720  <span style="color:#3a3a3a">░░░░░░░░░░░░░░░░░░░░</span>           │
│ elapsed                    │ 00:00:00                                │
│ last update                │ 2026-09-24 23:14:01                     │
│ last commit                │ #0                                      │
├────────────────────────────┼──────────┼──────────┼───────────────────┤
│ model                      │ <span style="color:#3a3a3a">CER</span>      │ <span style="color:#3a3a3a">exact%</span>   │ <span style="color:#3a3a3a">bar</span>               │
│ <span style="color:#3a3a3a">no model results yet</span>                                                 │
│ <span style="color:#3a3a3a">pending models: gemma-3-270m-it, gemma-4-e4b, gemma-4-12b, bonsai-27b</span>│
├──────────────────────────────────────────────────────────────────────┤
│ <span style="color:#3a3a3a">by type</span>                                                             │
├────────────────────────────┼──────────┼──────────┼───────────────────┤
│ type                      │ <span style="color:#3a3a3a">CER</span>      │ <span style="color:#3a3a3a">exact%</span>   │ <span style="color:#3a3a3a">bar</span>               │
│ gibberish                  │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ prose                      │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ code                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ list                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ table                      │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ data                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
├──────────────────────────────────────────────────────────────────────┤
│ No model results yet - the run is just getting started.              │
└──────────────────────────────────────────────────────────────────────┘
</pre>

## Status (machine-readable)

```
loaded_model = None
samples_done = 0
total_samples = 720
elapsed_sec = 0.0
last_update = 2026-09-24 23:14:01
commit_num = 0
model.gemma-3-270m-it.cer = 0.0
model.gemma-3-270m-it.exact_pct = 0.0
model.gemma-3-270m-it.samples = 0
model.gemma-4-e4b.cer = 0.0
model.gemma-4-e4b.exact_pct = 0.0
model.gemma-4-e4b.samples = 0
model.gemma-4-12b.cer = 0.0
model.gemma-4-12b.exact_pct = 0.0
model.gemma-4-12b.samples = 0
model.bonsai-27b.cer = 0.0
model.bonsai-27b.exact_pct = 0.0
model.bonsai-27b.samples = 0
```

## Conclusion

No model results yet - the run is just getting started.
