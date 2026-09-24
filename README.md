<h1 align="center"><span style="color:#ff0044">C</span><span style="color:#ff6a00">o</span><span style="color:#ffb300">p</span><span style="color:#00cc66">y</span><span style="color:#00aaff">C</span><span style="color:#7a5cff">h</span><span style="color:#d500f9">e</span><span style="color:#ff0044">c</span><span style="color:#ff6a00">k</span></h1>

**Aim:** Measure how accurately local LLMs reproduce input text verbatim.

**Why:** Verbatim copy fidelity is the tightest probe of a model's raw reliability for text-editing tasks.

## Live Status

<pre>
┌─CopyCheck live run · refresh: every cell commit (~72 pushes / run)───┐
├────────────────────────────┬─────────────────────────────────────────┤
│ loaded model               │ gemma-3-270m-it                         │
│ samples done               │ 2 / 720  <span style="color:#3a3a3a">░░░░░░░░░░░░░░░░░░░░</span>           │
│ elapsed                    │ 00:00:11                                │
│ last update                │ 2026-09-24 23:01:29                     │
│ last commit                │ #2                                      │
├────────────────────────────┼──────────┼──────────┼───────────────────┤
│ model                      │ <span style="color:#3a3a3a">CER</span>      │ <span style="color:#3a3a3a">exact%</span>   │ <span style="color:#3a3a3a">bar</span>               │
│ gemma-3-270m-it            │ <span style="color:#ff4d4d">48.56%</span>   │ <span style="color:#00cc66">0.00%</span>    │ <span style="color:#ff4d4d">███████</span><span style="color:#3a3a3a">░░░░░░░</span>    │
│ <span style="color:#3a3a3a">pending models: gemma-4-e4b, gemma-4-12b, bonsai-27b</span>                 │
├──────────────────────────────────────────────────────────────────────┤
│ <span style="color:#3a3a3a">by type</span>                                                             │
├────────────────────────────┼──────────┼──────────┼───────────────────┤
│ type                      │ <span style="color:#3a3a3a">CER</span>      │ <span style="color:#3a3a3a">exact%</span>   │ <span style="color:#3a3a3a">bar</span>               │
│ gibberish                  │ <span style="color:#ff4d4d">48.56%</span>   │ <span style="color:#00cc66">0.00%</span>    │ <span style="color:#ff4d4d">███████</span><span style="color:#3a3a3a">░░░░░░░</span>    │
│ prose                      │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ code                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ list                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ table                      │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
│ data                       │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">—</span>        │ <span style="color:#3a3a3a">░░░░░░░░░░░░░░</span>    │
├──────────────────────────────────────────────────────────────────────┤
│ Best copier so far: gemma-3-270m-it @ 48.56% CER (0.0% exact).       │
│ Worst: gemma-3-270m-it @ 48.56%. Not yet dependable for verbatim     │
│ work — proofread befor…                                              │
└──────────────────────────────────────────────────────────────────────┘
</pre>

## Status (machine-readable)

```
loaded_model = gemma-3-270m-it
samples_done = 2
total_samples = 720
elapsed_sec = 11.475276470184326
last_update = 2026-09-24 23:01:29
commit_num = 2
model.gemma-3-270m-it.cer = 0.4855952380952381
model.gemma-3-270m-it.exact_pct = 0.0
model.gemma-3-270m-it.samples = 2
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

Best copier so far: gemma-3-270m-it @ 48.56% CER (0.0% exact). Worst: gemma-3-270m-it @ 48.56%. Not yet dependable for verbatim work — proofread before applying edits.
