import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",   // static export for Cloudflare Pages
  trailingSlash: true, // ensures /scanner/ works without a server
};

export default nextConfig;
