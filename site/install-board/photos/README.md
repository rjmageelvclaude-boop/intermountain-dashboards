# Installer photos

The ServiceTitan API does not expose technician profile photos, so the board
uses initials avatars until a photo is dropped in this folder.

Add one JPG per tech, named `<company>-<technicianId>.jpg`:

    sierra-528566460.jpg
    ultimate-12345678.jpg
    russett-87654321.jpg

The technician id is in the `id` field of each tech row in `../data.json`
(or the ServiceTitan URL when viewing the technician in People > Technicians).
Portrait-ish crops look best; the card crops to fit automatically.
