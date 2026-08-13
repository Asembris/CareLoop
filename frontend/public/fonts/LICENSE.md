# Self-hosted fonts

Eight `woff2` files, byte-identical to what `fonts.gstatic.com` serves. They are committed rather
than fetched at page load because the CareLoop demo is filmed once: a bad moment of network would
render the whole editorial identity in fallback faces, and there is no retake of a live agent run.

Only the `latin` and `latin-ext` subsets are carried. The app is English; the cyrillic and
vietnamese subsets Google also serves would be a quarter of a megabyte that nothing renders.

Captured 2026-08-13 from the stylesheet the removed `index.html` link requested:

```
https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Newsreader:opsz,wght@6..72,300;6..72,400&family=IBM+Plex+Mono:wght@400;500&display=swap
```

| File | Family | Weights | Upstream |
|---|---|---|---|
| `newsreader-latin.woff2` | Newsreader | 300–400 (variable) | `/s/newsreader/v26/cY9AfjOCX1hbuyalUrK4397yjA.woff2` |
| `newsreader-latin-ext.woff2` | Newsreader | 300–400 (variable) | `/s/newsreader/v26/cY9AfjOCX1hbuyalUrK439DyjJBG.woff2` |
| `instrument-sans-latin.woff2` | Instrument Sans | 400–600 (variable) | `/s/instrumentsans/v4/…ibnr0SZe1Q.woff2` |
| `instrument-sans-latin-ext.woff2` | Instrument Sans | 400–600 (variable) | `/s/instrumentsans/v4/…ibnr0She1YmV.woff2` |
| `ibm-plex-mono-400-latin.woff2` | IBM Plex Mono | 400 | `/s/ibmplexmono/v20/-F63fjptAgt5VM-kVkqdyU8n1i8q1w.woff2` |
| `ibm-plex-mono-400-latin-ext.woff2` | IBM Plex Mono | 400 | `/s/ibmplexmono/v20/-F63fjptAgt5VM-kVkqdyU8n1iEq129k.woff2` |
| `ibm-plex-mono-500-latin.woff2` | IBM Plex Mono | 500 | `/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3twJwlBFgg.woff2` |
| `ibm-plex-mono-500-latin-ext.woff2` | IBM Plex Mono | 500 | `/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3twJwl5FgtIU.woff2` |

Newsreader and Instrument Sans are variable fonts — one file per subset covers the whole weight
range, which is why `src/styles/fonts.css` declares them with a weight range rather than once per
weight. IBM Plex Mono is static, so its two weights are two files.

## Licence

All three families are licensed under the **SIL Open Font License, Version 1.1**, which permits
redistribution and embedding, including in this repository once it goes public. CareLoop's own code
is Apache-2.0; these files are not, and this note is what keeps the two from being confused.

- **Newsreader** — Copyright 2019 The Newsreader Project Authors
  (<https://github.com/productiontype/Newsreader>)
- **Instrument Sans** — Copyright 2023 The Instrument Sans Project Authors
  (<https://github.com/Instrument/instrument-sans>)
- **IBM Plex Mono** — Copyright 2017 IBM Corp. (<https://github.com/IBM/plex>)

The families are unmodified: subsetting here means selecting which of the publisher's own subset
files to carry, not re-generating a font. Nothing was renamed — the CSS `font-family` names are the
Reserved Font Names, used to refer to the originals, which is what OFL clause 3 permits.

---

```
Copyright (c) <dates>, <copyright holders> (<URL|email>),
with Reserved Font Name <Reserved Font Name>.

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
https://openfontlicense.org

-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical writer
or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```
