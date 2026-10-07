/** @type {import('next').NextConfig} */
const nextConfig = {
  // The desktop app ships the dashboard as static files (Electron loads them from disk); there is no
  // Next.js server in production. Everything dynamic happens in the browser against the local FastAPI backend.
  output: "export",
  trailingSlash: true, // /cores -> cores/index.html, which the Electron static protocol can resolve
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
