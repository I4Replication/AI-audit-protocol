# House citations

Every I4R comment cites the two protocol documents it was produced under. `run_coordinator.py` and
`check_consistency.sh` reject a report that lacks the markers `AI-Games` and `AI-vertical`; this
file is the single canonical source of the reference text, and the coordinator prompt carries it
verbatim so the model never has to invent a citation. Edit the text here, nowhere else.

## Reference entries

Write both entries in `## References`, with these exact anchors so `build_docx.sh` colours the
in-text links as citations:

```
[]{#ref-AI-Games}Brodeur, A., Valenta, D., Marcoci, A., Aparicio, J. P., Mikola, D., Barbarioli, B., Alexander, R., Deer, L., Stafford, T., Vilhuber, L., Bensch, G., et al. (2026). AI-assisted teams outperform AI-led teams but not human-only teams in assessing research reproducibility in quantitative social science. *Proceedings of the National Academy of Sciences*, 123(22), e2524747123. <https://doi.org/10.1073/pnas.2524747123> · <https://github.com/I4Replication/AI-Games>

[]{#ref-AI-vertical}Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. Pre-analysis plan, OSF: <https://osf.io/dkfzt/> · <https://github.com/I4Replication/AI-vertical>
```

## In-text use

Cite both once in the Introduction as the protocol the comment follows, in this form:

> This comment was produced under the Institute for Replication's AI-assisted reproducibility
> protocol ([Brodeur et al., 2026](#ref-AI-Games); [Institute for Replication, 2026](#ref-AI-vertical)).

## Byline

Published comments use the named team, not a collective byline:

```
::: {custom-style="Authors"}
Ghina Abdul Baki^1^, Juan P. Aparicio^1^\*, Bruno Barbarioli^1^, Abel Brodeur^1^, David Valenta^1^
:::
::: {custom-style="Affil"}
^1^ Institute for Replication, Department of Economics, University of Ottawa
:::
::: {custom-style="Affil"}
\* Correspondence: Juan P. Aparicio, juposada93@gmail.com
:::
```
