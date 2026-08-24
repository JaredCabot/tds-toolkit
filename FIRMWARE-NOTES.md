# Reading a TDS firmware image

Working notes, not part of the program. The goal is to read each
instrument's SCPI command tree out of its firmware, so what a given model
can do is known without having one on the bench.

## The images

67 files in the firmware folder are **34 distinct binaries**. One image was
shipped for a whole family, so measuring one instrument settles the rest:

| image | size | symbols | models |
|---|---|---|---|
| `15a07eac7f` | 4 MB | yes | TDS520C, 540C, 580C, 754C, **784C** — all v5.3e |
| `7e80aad56c` | 4 MB | yes | TDS714L, 754D, **784D** — all v7.4e |
| `2e7d2cfdac` | 4 MB | yes | TDS520C, 540C, 580C, 754C, 784C — all v5.0e |
| `633b691731` | 4 MB | yes | TDS754D, 784D, 794D — all v6.0e |
| `d5b6022c04` | 4 MB | yes | **TDS640A** v3.8.8e |
| `d82e9f9ead` | 1.5 MB | **no** | TDS520 v2.16e |
| `77619de137` | 1.5 MB | **no** | TDS540 v2.16e |
| `ed3384d89e` | 1.5 MB | **no** | TDS620 v2.04e |
| `42fec8e5a3` | 4 MB | **no** | TDS820 v2.03 |

30 of the 34 carry a symbol table. The four that do not are the earliest
firmware — the original TDS500/600 at 1.5 MB, and the TDS820 sampling
scope, which is a different instrument entirely.

Bold entries are the three instruments measured on the bench, so any
decoding has to reproduce: the 640A has **no** `READFILE` and **no**
`WRITEFILE`; the 784C and 784D have both.

`fwindex.py` in the diagnostics regenerates this table.

## The symbol table

This is the way in, and it was not obvious: no SCPI command mnemonic
appears as text anywhere in any image. `FILESYSTEM`, `READFILE`,
`WRITEFILE`, `FREESPACE`, `MKDIR` all return zero hits — checked as
literals, as case variants, as high-bit-terminated strings, and against
every XOR key from 1 to 255.

What is there is a VxWorks symbol table, which names every function and
variable in the firmware.

**In `TDS784D_v7.4e` (image `7e80aad56c`):**

```
packed names   file 0x34F048 .. 0x36A492   7165 names, NUL-separated, sorted
symbol array   file 0x3B668C .. 0x3D264C   7164 entries of 16 bytes
load base      0x05001000                  runtime address - base = file offset
```

Entry layout, big-endian:

```
+0  uint32  pointer to the name
+4  uint32  the symbol's value
+8  uint32  type    0x500 text, 0x700 and 0x900 data
+12 uint32  zero
```

The array was found by its own arithmetic rather than by guessing a base:
the names are packed end to end, so consecutive name pointers must differ
by exactly each name's length plus one. Searching for that delta sequence
finds the array and yields the load base in one step — no assumption about
where the ROM is mapped.

### Symbols that matter

```
_ParseRoot            0x053B64E0    the parser's root structure
_current_node         0x055ADF44
_node_ids             0x05573E10
_MatchKeywordPars     0x05326232
_GotoRootGrun         0x05312A0C    tree walk: root
_GotoParentNodeGrun                 tree walk: up
_GotoSuccessiveNodeGrun             tree walk: next sibling
_GotoAlternateNodeGrun              tree walk: alternative
_GetReadActionGrun    0x05312592    a node's query handler
_GetWriteActionGrun   0x05312640    a node's set handler
```

The parser's action names are also in plain text at file `0x318A40`:
`STARTATROOTACTION`, `TREEWALKIDACTION`, `BEGINLEAFACTION`,
`SEARCHENUMLEAFACTION`, `PUSHLEAFACTION`, `DONEMESSAGEACTION`. So it is a
table-driven tree walker, which is exactly why no mnemonic exists as
contiguous bytes: a command is a **path through nodes**, assembled a
fragment at a time as the walker matches input.

The parser framework itself is identical across images — of 245
`*Grun`/`*Pars` symbols, the 640A and 784D differ by four, none of them
file-related. The capability difference lives entirely in the tree data.

## The keyword table

Every mnemonic is in the image after all, held as a shared character pool
with the letters folded. Six bits of character, two bits of flag:

```
byte & 0x3F    0x0A         the '*' of a common command
               0x10-0x19    digits 0-9
               0x21-0x3A    letters, as (char | 0x60)
byte & 0x80    set on the optional tail of the SCPI long form
```

That last bit is the whole convention the manuals print in mixed case:
`ACQuire` means `ACQ` is enough to type. It is in the ROM as a flag bit.

Two tables, found by shape rather than by symbol, so this works on images
with no symbol table at all:

```
_KwdIdxTbl    uint16 boundaries into the character pool, one per keyword
_KwdCharTbl   the pool itself, immediately after the index table
```

The index table always opens `0, 4, 8, 12, 16, 20, 24, 28`, because the
first eighteen keywords of every TDS are the IEEE 488.2 common commands -
`*CAL`, `*CLS`, `*DDT` ... - and every one of them is four characters
long. That is signature enough to find it, and it is unique in all 34
images. The end of the index table is found by trimming entries until the
pool that follows is exactly as long as the last boundary says and
contains only plausible characters.

`tekkwd.py` in the diagnostics does all of this. Point it at a folder and
it reports every distinct image.

## What it says

All 34 images, in three clear generations:

| generation | filesystem | download | upload |
|---|---|---|---|
| v2.x — TDS520, 540, 620 | **none at all** | no | no |
| A and B series, v3.x and v4.x | yes | `PRINT` | **no** |
| C and D series, v5.0e and later | yes | `READFILE` | yes |

The 640A is not the odd one out after all — it is the **majority**. Every
A-series and B-series instrument has the same gap: browse and download,
never upload. And the original v2.x firmware has no `FILESYSTEM` subsystem
whatsoever: no `CWD`, no `DIR`, no `FREESPACE`, nothing to browse.

The only image that will not decode is the TDS820 at v2.03, which is a
sampling oscilloscope and a different instrument altogether.

## Where this stops

`_ParseRoot + 0` points at `0x05330068`. That is not a table of pointers:

```
05330068  61 04 78 04 46 d3 fb 04 77 fb 04 46 de fb 05 85
05330078  05 45 c2 17 05 45 85 05 84 fb 20 1b 05 6b fb 20
```

It is a byte-oriented encoding — variable-length records with opcodes,
not fixed-size structs. Decoding it is the remaining work, and the way in
is `_MatchKeywordPars` at `0x05326232`: whatever that function reads is
the node format, and it is short enough to read as 68k by hand.

## Validation

The decoding was held to reproducing, unprompted, what three instruments
on the bench had already said. It does:

1. `READFILE` and `WRITEFILE` absent from `d5b6022c04` (640A) and present
   in `7e80aad56c` (784D) and `15a07eac7f` (784C).
2. The fifteen-command matrix `capabilities.py` measured on the bench —
   `DIR`, `DELETE`, `RENAME`, `COPY`, `MKDIR`, `RMDIR`, `PRINT`,
   `OVERWRITE`, `DELWARN` present on all three; `MOUNT` and `UNMOUNT`
   absent from all three.
3. No size query on any of them, which the bench probe established
   against eight plausible spellings. The keyword table has a `SIZe`, but
   the bench proved it is not a file size — it belongs to another
   subsystem. A flat keyword list says what words exist, not where they
   sit in the tree, and that distinction is worth keeping in mind before
   reading too much into any single entry.

If a decoding disagrees with the instruments, the decoding is wrong.
