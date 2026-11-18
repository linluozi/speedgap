# Release notes

The extra index pass relaxes the matrix scan and fixes the tax on nested
documents. Box parsing is now linear in the number of tokens, and the next
release will expose the voxel cache as a public option.

- [x] relax the index bound
- [x] cache the matrix
- [ ] audit the vortex path

Existing callers do not need to change anything; the old syntax keeps working,
including the `xenon` alias used by the export tool.
