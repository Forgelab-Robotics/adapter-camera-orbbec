# Changelog

All notable changes to this project are documented in this file.

The project follows Semantic Versioning. Dates use the `YYYY-MM-DD` format.

## Unreleased

### Added

- Added optional Forge PointCloud v1 Dora output with organized XYZ/XYZRGB semantics, optical-frame and invalid-point rules, reference configuration, hardware-validation guidance, and `PointCloudView` example routing.

### Changed

- Updated to `forge-msgs==1.1.0`; point-cloud processing uses the published Depth frame, detaches SDK buffers into immutable columns, and fails open so Color/Depth/IR continue when the optional derived output is unavailable.
- Documented the unavoidable multiprocessing serialization copy in `capture_process: isolated`; NumPy-to-Arrow uses `copy="never"` only after the frame reaches the parent process.

### Removed

- Removed the optional security-policy and code-of-conduct files to align the public documentation set across adapters.

## 1.0.1 - 2026-08-15

### Added

- Apache-2.0 licensing and public project metadata.
- Contributor, security, conduct, release, privacy, and continuous-integration documentation.
- Strict validation for raw Color/IR frame buffer sizes and configuration booleans.

### Changed

- Replaced internal Forge Git dependencies with the published `forge-common` and `forge-msgs` packages.

### Removed

- Removed environment-specific hardware baseline and parameter validation reports from the public tree.

## 1.0.0 - 2026-07-13

- Initial Orbbec Gemini 2 release with Color, Depth and IR capture, Dora streaming, snapshots, isolated SDK capture, environment checks and PyInstaller packaging.
