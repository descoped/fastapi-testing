# Release Management Process

This document describes the release management process for the `fastapi-testing` project.

## Overview

Releases are managed through GitHub Releases with automated PyPI publishing via GitHub Actions. The process follows semantic versioning (SemVer) with `v` prefixed tags.

## Release Process

### 1. Pre-Release Preparation

Before creating a release, ensure:

- [ ] All intended changes are merged into the `master` branch
- [ ] Tests are passing on the latest commit
- [ ] Version in `pyproject.toml` matches the intended release version
- [ ] Documentation is up to date

### 2. Version Management

#### Semantic Versioning
Follow [SemVer](https://semver.org/) guidelines:
- **MAJOR** (`v1.0.0 → v2.0.0`): Breaking changes
- **MINOR** (`v1.0.0 → v1.1.0`): New features, backward compatible
- **PATCH** (`v1.0.0 → v1.0.1`): Bug fixes, backward compatible

#### Update Version
Update the version in `pyproject.toml`:
```toml
[project]
version = "1.2.3"  # New version without 'v' prefix
```

Commit this change:
```bash
git add pyproject.toml
git commit -m "Bump version to 1.2.3"
git push origin master
```

### 3. Creating a Release

1. **Navigate to Releases**
   - Go to the repository on GitHub
   - Click on "Releases" in the right sidebar
   - Click "Create a new release"

2. **Configure the Release**
   - **Tag**: Enter the new tag (e.g., `v1.2.3`)
     - ⚠️ **Must follow pattern**: `v[0-9]+.[0-9]+.[0-9]+`
     - ✅ Valid: `v1.2.3`, `v0.1.0`, `v10.5.2`
     - ❌ Invalid: `1.2.3`, `v1.2`, `v1.2.3-beta`
   - **Target**: Select `master` branch
   - **Title**: `v1.2.3` (same as tag)
   - **Description**: Add release notes (see template below)

3. **Publish the Release**
   - Click "Publish release"
   - This triggers the automated workflow

### 4. Automated Workflow

The GitHub Action workflow (`.github/workflows/release.yml`) automatically:

1. **Validates** the release:
   - Checks tag format matches regex `^v[0-9]+\.[0-9]+\.[0-9]+$`
   - Extracts version number (removes `v` prefix)
   - Verifies `pyproject.toml` version matches tag version

2. **Runs quality checks**:
   - Full test suite

3. **Builds and publishes**:
   - Builds the package with `uv build`
   - Generates artifact attestation for supply chain security
   - Publishes to PyPI with `uv publish`

### 5. Post-Release Verification

After the workflow completes:

- [ ] Check the [PyPI page](https://pypi.org/project/fastapi-testing/) for the new version
- [ ] Test installation: `uv add fastapi-testing==1.2.3`
- [ ] Verify the GitHub Action workflow succeeded
- [ ] Monitor for any issues or bug reports

## Release Notes Template

Use this template for release descriptions:

```markdown
## What's Changed

### 🚀 New Features
- Feature description

### 🐛 Bug Fixes  
- Bug fix description

### 📚 Documentation
- Documentation updates

### 🔧 Internal Changes
- Internal improvements

### 📦 Dependencies
- Dependency updates

**Full Changelog**: https://github.com/descoped/fastapi-testing/compare/v1.2.2...v1.2.3
```

## Troubleshooting

### Common Issues

#### Version Mismatch Error
```
❌ Version mismatch!
   pyproject.toml version: 1.2.2
   Git tag version: 1.2.3
```

**Solution**: Update `pyproject.toml` version to match the git tag, then create a new release.

#### Invalid Tag Format
```
❌ Invalid tag format: 1.2.3
Expected format: v[0-9]+.[0-9]+.[0-9]+ (e.g., v1.2.3)
```

**Solution**: Ensure the tag starts with `v` and follows semantic versioning.

#### PyPI Token Issues
```
❌ Authentication failed
```

**Solution**: Verify the `PYPI_API_TOKEN` secret is correctly configured in the repository settings.

### Manual Release Recovery

If the automated workflow fails after creating the GitHub release:

1. **Check the workflow logs** for specific error details
2. **Fix the issue** (version mismatch, test failures, etc.)
3. **Delete the failed release** and tag from GitHub
4. **Create a new release** with the corrected information

### Emergency Hotfix Process

For critical bug fixes:

1. Create a hotfix branch from the latest release tag
2. Apply the fix and update the patch version
3. Follow the standard release process
4. Consider backporting to maintenance branches if needed

## Security Considerations

- **API Token**: Uses project-scoped PyPI API token stored in GitHub Secrets
- **Attestations**: Workflow generates artifact attestations for supply chain security  
- **Environment Protection**: Release job uses `pypi` environment for additional security
- **Permissions**: Workflow uses minimal required permissions with OIDC token

## Monitoring

Monitor releases through:
- GitHub Actions workflow status
- PyPI download statistics
- GitHub release metrics
- Community feedback and issue reports