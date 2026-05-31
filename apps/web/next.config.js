/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Proxy /api/* → backend during dev to avoid CORS preflight surprises.
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: (process.env.ATLAS_API_URL || 'http://localhost:8000') + '/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
