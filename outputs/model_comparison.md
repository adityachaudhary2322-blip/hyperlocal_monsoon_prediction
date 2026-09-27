# Model comparison - Phase 4

Pilot districts only. Test years **2022-2026**. BSS is against the same
climatology baseline throughout, so the columns are directly comparable.

Models: climatology; the Phase 3 calibrated LightGBM; and two stackers -
a logistic regression on the LightGBM log-odds plus Chronos-2 weekly
quantile totals (weeks 1-4, q10/q50/q90), one using zero-shot Chronos-2 and
one using a LoRA fine-tune on data up to 2018.

## onset

| horizon | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack | events |
|---:|---:|---:|---:|---:|---:|
| 7d | +0.000 | +0.032 | +0.013 | +0.008 | 194 |
| 14d | +0.000 | +0.001 | -0.140 | -0.167 | 380 |
| 21d | +0.000 | -0.014 | -0.356 | -0.422 | 545 |
| 28d | +0.000 | -0.076 | -0.399 | -0.435 | 703 |

### Per state (BSS)

| horizon | state | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack |
|---:|---|---:|---:|---:|---:|
| 7d | Bihar | +0.000 | -0.039 | -0.038 | -0.063 |
| 7d | Madhya Pradesh | +0.000 | +0.090 | +0.084 | +0.098 |
| 7d | Maharashtra | +0.000 | +0.014 | -0.008 | -0.029 |
| 7d | Uttar Pradesh | +0.000 | +0.002 | -0.033 | -0.032 |
| 14d | Bihar | +0.000 | -0.116 | -0.086 | -0.208 |
| 14d | Madhya Pradesh | +0.000 | +0.112 | -0.019 | -0.006 |
| 14d | Maharashtra | +0.000 | -0.013 | -0.225 | -0.263 |
| 14d | Uttar Pradesh | +0.000 | -0.099 | -0.145 | -0.187 |
| 21d | Bihar | +0.000 | -0.127 | -0.121 | -0.207 |
| 21d | Madhya Pradesh | +0.000 | +0.025 | -0.180 | -0.160 |
| 21d | Maharashtra | +0.000 | -0.030 | -0.598 | -0.736 |
| 21d | Uttar Pradesh | +0.000 | -0.006 | -0.102 | -0.118 |
| 28d | Bihar | +0.000 | -0.270 | -0.312 | -0.318 |
| 28d | Madhya Pradesh | +0.000 | -0.151 | -0.150 | -0.104 |
| 28d | Maharashtra | +0.000 | -0.077 | -0.695 | -0.801 |
| 28d | Uttar Pradesh | +0.000 | +0.025 | -0.066 | -0.049 |

## dry

| horizon | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack | events |
|---:|---:|---:|---:|---:|---:|
| 7d | +0.000 | +0.032 | +0.023 | +0.014 | 884 |
| 14d | +0.000 | +0.019 | -0.022 | -0.029 | 1286 |
| 21d | +0.000 | +0.004 | -0.106 | -0.112 | 1626 |
| 28d | +0.000 | -0.010 | -0.128 | -0.135 | 1916 |

### Per state (BSS)

| horizon | state | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack |
|---:|---|---:|---:|---:|---:|
| 7d | Bihar | +0.000 | -0.038 | +0.042 | +0.031 |
| 7d | Madhya Pradesh | +0.000 | +0.049 | +0.087 | +0.091 |
| 7d | Maharashtra | +0.000 | +0.068 | +0.034 | +0.015 |
| 7d | Uttar Pradesh | +0.000 | -0.074 | -0.096 | -0.095 |
| 14d | Bihar | +0.000 | -0.053 | -0.004 | -0.015 |
| 14d | Madhya Pradesh | +0.000 | +0.025 | +0.065 | +0.081 |
| 14d | Maharashtra | +0.000 | +0.061 | -0.012 | -0.037 |
| 14d | Uttar Pradesh | +0.000 | -0.106 | -0.198 | -0.191 |
| 21d | Bihar | +0.000 | -0.028 | -0.115 | -0.186 |
| 21d | Madhya Pradesh | +0.000 | +0.022 | -0.026 | +0.003 |
| 21d | Maharashtra | +0.000 | +0.032 | -0.082 | -0.106 |
| 21d | Uttar Pradesh | +0.000 | -0.106 | -0.316 | -0.307 |
| 28d | Bihar | +0.000 | -0.024 | -0.101 | -0.165 |
| 28d | Madhya Pradesh | +0.000 | +0.017 | -0.022 | -0.012 |
| 28d | Maharashtra | +0.000 | +0.012 | -0.102 | -0.124 |
| 28d | Uttar Pradesh | +0.000 | -0.122 | -0.398 | -0.376 |

## heavy

| horizon | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack | events |
|---:|---:|---:|---:|---:|---:|
| 7d | +0.000 | +0.020 | +0.043 | +0.039 | 212 |
| 14d | +0.000 | -0.007 | +0.016 | +0.020 | 399 |
| 21d | +0.000 | -0.033 | +0.005 | +0.014 | 565 |
| 28d | +0.000 | -0.053 | -0.018 | -0.010 | 721 |

### Per state (BSS)

| horizon | state | climatology | lightgbm | chronos_zeroshot_stack | chronos_finetuned_stack |
|---:|---|---:|---:|---:|---:|
| 7d | Bihar | +0.000 | -0.015 | +0.015 | +0.008 |
| 7d | Madhya Pradesh | +0.000 | +0.014 | +0.067 | +0.066 |
| 7d | Maharashtra | +0.000 | +0.036 | +0.040 | +0.029 |
| 7d | Uttar Pradesh | +0.000 | +0.011 | +0.001 | +0.005 |
| 14d | Bihar | +0.000 | -0.026 | +0.001 | +0.011 |
| 14d | Madhya Pradesh | +0.000 | -0.045 | -0.006 | -0.007 |
| 14d | Maharashtra | +0.000 | +0.035 | +0.049 | +0.053 |
| 14d | Uttar Pradesh | +0.000 | +0.007 | +0.013 | +0.024 |
| 21d | Bihar | +0.000 | -0.028 | -0.018 | +0.001 |
| 21d | Madhya Pradesh | +0.000 | -0.079 | -0.010 | -0.006 |
| 21d | Maharashtra | +0.000 | -0.006 | -0.001 | +0.007 |
| 21d | Uttar Pradesh | +0.000 | +0.005 | +0.047 | +0.064 |
| 28d | Bihar | +0.000 | -0.031 | -0.013 | +0.021 |
| 28d | Madhya Pradesh | +0.000 | -0.100 | -0.036 | -0.025 |
| 28d | Maharashtra | +0.000 | -0.017 | -0.014 | -0.009 |
| 28d | Uttar Pradesh | +0.000 | -0.040 | +0.006 | +0.005 |

## Model selection, and why it failed

Selecting on validation years agreed with the test years in **3 of 12** cases.

That is the most useful result in this phase. LightGBM scores +0.19 to +0.21 BSS on the validation years and *negative* BSS on the test years, because Phase 3 fitted both its early stopping and its isotonic calibrator on exactly those years. Anyone selecting a model on that number ships the wrong one.

| target | horizon | chosen on validation | val BSS | its test BSS | best on test | test BSS |
|---|---:|---|---:|---:|---|---:|
| onset | 7d | `lightgbm` | +0.0297 | +0.0315 | `lightgbm` | +0.0315 |
| onset | 14d | `climatology` | +0.0000 | +0.0000 | `lightgbm` | +0.0008 |
| onset | 21d | `climatology` | +0.0000 | +0.0000 | `climatology` | +0.0000 |
| onset | 28d | `climatology` | +0.0000 | +0.0000 | `climatology` | +0.0000 |
| dry | 7d | `chronos_finetuned_stack` | +0.0919 | +0.0142 | `lightgbm` | +0.0319 |
| dry | 14d | `chronos_finetuned_stack` | +0.0678 | -0.0294 | `lightgbm` | +0.0188 |
| dry | 21d | `chronos_finetuned_stack` | +0.0408 | -0.1118 | `lightgbm` | +0.0044 |
| dry | 28d | `chronos_zeroshot_stack` | +0.0267 | -0.1276 | `climatology` | +0.0000 |
| heavy | 7d | `lightgbm` | +0.0178 | +0.0197 | `chronos_zeroshot_stack` | +0.0430 |
| heavy | 14d | `lightgbm` | +0.0268 | -0.0072 | `chronos_finetuned_stack` | +0.0197 |
| heavy | 21d | `lightgbm` | +0.0432 | -0.0327 | `chronos_finetuned_stack` | +0.0137 |
| heavy | 28d | `lightgbm` | +0.0284 | -0.0531 | `climatology` | +0.0000 |

Recorded in [`config/model_choice.yaml`](../config/model_choice.yaml), which carries both columns and a warning at the top.

### How much the stacker flatters itself

The stacker is trained on the validation years, so its fit to those rows is not evidence. Leave-one-year-out across 2019-2021 is:

| model | target | horizon | in-sample BSS | leave-one-year-out BSS |
|---|---|---:|---:|---:|
| chronos_zeroshot_stack | onset | 7d | +0.050 | +0.004 |
| chronos_zeroshot_stack | onset | 14d | +0.115 | -0.191 |
| chronos_zeroshot_stack | onset | 21d | +0.167 | -0.295 |
| chronos_zeroshot_stack | onset | 28d | +0.230 | -0.184 |
| chronos_zeroshot_stack | dry | 7d | +0.111 | +0.072 |
| chronos_zeroshot_stack | dry | 14d | +0.189 | +0.050 |
| chronos_zeroshot_stack | dry | 21d | +0.195 | +0.037 |
| chronos_zeroshot_stack | dry | 28d | +0.186 | +0.027 |
| chronos_zeroshot_stack | heavy | 7d | +0.047 | -0.024 |
| chronos_zeroshot_stack | heavy | 14d | +0.062 | -0.010 |
| chronos_zeroshot_stack | heavy | 21d | +0.081 | -0.007 |
| chronos_zeroshot_stack | heavy | 28d | +0.073 | -0.052 |
| chronos_finetuned_stack | onset | 7d | +0.062 | -0.022 |
| chronos_finetuned_stack | onset | 14d | +0.153 | -0.113 |
| chronos_finetuned_stack | onset | 21d | +0.210 | -0.223 |
| chronos_finetuned_stack | onset | 28d | +0.228 | -0.275 |
| chronos_finetuned_stack | dry | 7d | +0.119 | +0.092 |
| chronos_finetuned_stack | dry | 14d | +0.192 | +0.068 |
| chronos_finetuned_stack | dry | 21d | +0.197 | +0.041 |
| chronos_finetuned_stack | dry | 28d | +0.186 | +0.012 |
| chronos_finetuned_stack | heavy | 7d | +0.055 | -0.011 |
| chronos_finetuned_stack | heavy | 14d | +0.060 | -0.012 |
| chronos_finetuned_stack | heavy | 21d | +0.074 | -0.040 |
| chronos_finetuned_stack | heavy | 28d | +0.061 | -0.090 |

## Caveats

- **The stacker trains on three years.** 2019-2021 is 2,520 rows before any target-specific NaN masking, against 13 features. That is thin.
- **Its LightGBM input is optimistic on exactly those years**, because Phase 3 fitted the isotonic calibrator on them. The stack learns to trust a signal that is better in training than at test time.
- **Chronos-2 never sees the targets.** It forecasts daily rainfall; the weekly quantiles are summaries of that forecast. Summing daily quantiles is not the quantile of a weekly total, and these are used as features only.
- District slices with fewer than 10 events are in `metrics.csv` but should not be read as skill.
