# Corpus sources

| doc_id | Document | Regulator | Version | Official source |
|---|---|---|---|---|
| SEBI-SCORES-MC | Master Circular on redressal of investor grievances through SCORES and linking to ODR (SEBI/HO/OIAE/IGRD/P/CIR/2023/156) | SEBI | 2023-09-20 | https://www.sebi.gov.in/legal/master-circulars/ |
| SEBI-MF-REGS | SEBI (Mutual Funds) Regulations, 1996: investor servicing provisions | SEBI | consolidated | https://www.sebi.gov.in/legal/regulations/ |
| RBI-IOS-2021 | Reserve Bank - Integrated Ombudsman Scheme, 2021 | RBI | 2021-11-12 | https://www.rbi.org.in |

## Format

`corpus/raw/*.txt` files are clause-level extracts. Each file starts with a header (`@doc_id`, `@regulator`, `@title`, `@reference`, `@version`, `@source_url`). Each clause starts with:

```
## [CLAUSE-ID] Title | type=<scope|exclusion|definition|grounds|maintainability|timeline|responsibility|obligation|procedure> | entities=<comma list>
```

The `type` and `entities` tags are metadata. The agent filters on them when it looks for scope, exclusion and timeline provisions.

**The wording here is condensed from the official texts.** Check every passage the reference case depends on against the official PDF before Evaluation 1. If you put an official PDF (with a text layer) in `corpus/raw/`, the pipeline indexes it too. It splits PDFs on numbered headings and falls back to fixed windows. A scanned PDF needs OCR first, for example `ocrmypdf in.pdf out.pdf`.

## Passages the reference case (section 4.1) depends on

| Passage | Why it matters |
|---|---|
| RBIOS-2 | Banks are Regulated Entities covered by the Scheme |
| RBIOS-3A / RBIOS-3B | Definitions of Regulated Entity and deficiency in service. Short interest on an FD is a deficiency in service |
| RBIOS-8 | Grounds of complaint: a customer aggrieved by an act or omission of a Regulated Entity |
| RBIOS-10B | Before going to the Ombudsman, the customer must complain in writing to the bank and wait 30 days |
| SCORES-2 | SEBI does not deal with complaints against entities it does not regulate |

The complaint never mentions the RBI provisions. It names SEBI and uses investment vocabulary, so a plain search returns SCORES-1 and SCORES-5.
