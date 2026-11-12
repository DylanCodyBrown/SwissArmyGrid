// esbuild bundles an imported stylesheet into a .css file beside its chunk; TypeScript only needs
// to know such an import exists.
declare module "*.css";
