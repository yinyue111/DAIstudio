const path = require("path");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  // Internal tool: allow loading remote reference / result images without the
  // next/image optimizer (we use plain <img>).
};

module.exports = nextConfig;
