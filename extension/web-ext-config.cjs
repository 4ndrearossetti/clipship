// web-ext configuration. The extension package shouldn't include the build
// script or developer-only files.
module.exports = {
  ignoreFiles: [
    "build.sh",
    "web-ext-config.cjs",
    "web-ext-artifacts",
  ],
};
