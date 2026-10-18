"""pdf2epub-voice: makes the voices of pdf2epub audiobooks on a computer with a GPU.

It connects out to a pdf2epub site (or the local app), takes audiobook jobs that are waiting for voices, reads
each book aloud with the engine and voice the user chose, builds an M4B with chapter marks and the cover, and
uploads it. See docs/audiobooks.md in the pdf2epub repository.
"""

__version__ = "0.1.0"
