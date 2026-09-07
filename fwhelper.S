| Page programmer for the TDS bootloader monitor, written for the 68k in the
| instrument. The host pokes this into scratch RAM with M writes and calls it
| with the monitor's B command, once per page.
|
| The B command's calling convention was read off the instrument's own monitor:
| one stack argument, a pointer to a four-byte response header, and the word at
| +4 of that is a pointer to the command body. The host's argument lands at
| body+12 and its payload at body+16.
|
|   body+12   the flash address this page belongs to
|   body+16   512 bytes of data
|
| Two entry points rather than a mode word in the payload, so the payload stays
| at the 512 bytes the monitor is known to carry:
|
|   +0   page buffer, Intel 28F016SA
|   +4   a longword at a time, command 0x40 - slower, and what the parts
|        without a page buffer need
|   +8   the same, on an array of four byte-wide parts rather than two
|        16-bit ones, where the status is a byte on every lane
|
| The response header's command byte is 'P' when the page went in and '-' when
| it did not, which is what the monitor sends back and what the host reads.
|
| Every command goes out in all four bytes of a longword, which is right for
| either arrangement: a 16-bit part decodes a command on its low byte and
| ignores the high one. Status does not work that way. Two 16-bit parts put
| it in the low byte of each half, 0x00800080; four byte-wide parts put it on
| every lane, 0x80808080. Polling the first pattern on the second watches two
| chips and takes their word for the other two, which is why there are two
| word entries rather than one.

        .text
        .globl  entry_page
        .globl  entry_word
        .globl  entry_word4

        .set    TRIES, 0x100000

entry_page:
        moveq   #0,%d0
        bra.b   common
entry_word:
        moveq   #1,%d0
        bra.b   common
entry_word4:
        moveq   #2,%d0
common:
        link.w  %a6,#0
        movem.l %d2-%d4/%a2-%a4,-(%sp)
        move.l  %d0,%d4                | which of the two ways in was taken
        movea.l 8(%a6),%a0              | the response header
        movea.l 4(%a0),%a1              | the command body
        movea.l 12(%a1),%a2             | this page's address in flash
        lea     16(%a1),%a3             | the 512 bytes
        clr.w   2(%a0)                  | the reply carries nothing after it
        clr.b   1(%a0)
        move.b  #0x2d,(%a0)             | '-' until it has worked
        tst.l   %d4
        bne.w   word_mode

| ---- mode 0: fill the page buffer, then commit it
        move.l  #0x71717171,(%a2)       | read extended status
        move.l  %a2,%d0                 | GSR sits at the device base
        andi.l  #0xffe00000,%d0
        addq.l  #8,%d0
        movea.l %d0,%a4
        move.l  #0x00040004,%d2
        move.l  %d2,%d3
        bsr.w   poll
        tst.l   %d0
        bmi.w   done

        move.l  #0xe0e0e0e0,(%a2)       | page buffer write
        move.l  #0x007f007f,(%a2)       | 128 longwords, low count
        move.l  #0x00000000,(%a2)       | high count
        movea.l %a2,%a4
        moveq   #127,%d2
1:      move.l  (%a3)+,(%a4)+
        dbra    %d2,1b
        move.l  #0xffffffff,(%a2)

        move.l  #0x71717171,(%a2)
        move.l  %a2,%d0                 | BSR sits at the block base
        andi.l  #0xfffe0000,%d0
        addq.l  #4,%d0
        movea.l %d0,%a4
        move.l  #0x00080008,%d2
        moveq   #0,%d3
        bsr.w   poll
        tst.l   %d0
        bmi.w   done

        move.l  #0x0c0c0c0c,(%a2)       | commit the page buffer
        move.l  #0x007f007f,(%a2)
        move.l  #0x00000000,(%a2)
        move.l  #0x71717171,(%a2)
        move.l  #0x00800080,%d2
        move.l  %d2,%d3
        bsr.w   poll                    | a4 is still the BSR
        tst.l   %d0
        bmi.b   done

        move.l  #0x50505050,(%a2)       | clear the status bits
        move.b  #0x50,(%a0)             | 'P'
        bra.b   done

| ---- mode 1: a longword at a time
word_mode:
        movea.l %a2,%a4
        move.l  #0x00800080,%d2         | two 16-bit parts
        cmpi.l  #2,%d4
        bne.b   5f
        move.l  #0x80808080,%d2         | four byte-wide ones
5:      move.l  %d2,%d3                 | poll leaves d2 and d3 alone
        move.w  #127,%d4
2:      move.l  #0x40404040,(%a4)
        move.l  (%a3)+,(%a4)
        bsr.b   poll
        tst.l   %d0
        bmi.b   done
        move.l  #0xffffffff,(%a2)
        addq.l  #4,%a4
        dbra    %d4,2b
        move.b  #0x50,(%a0)

done:
        move.l  #0xffffffff,(%a2)       | back to reading the array
        movem.l (%sp)+,%d2-%d4/%a2-%a4
        unlk    %a6
        rts

| a4 the address, d2 the mask, d3 what it has to read. d0 is 0, or -1 if it
| never got there - a bounded wait, so a flash that never answers hands the
| host a failure it can fall back from rather than wedging the instrument.
poll:
        move.l  #TRIES,%d1
3:      move.l  (%a4),%d0
        and.l   %d2,%d0
        cmp.l   %d3,%d0
        beq.b   4f
        subq.l  #1,%d1
        bne.b   3b
        moveq   #-1,%d0
        rts
4:      moveq   #0,%d0
        rts
