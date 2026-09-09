import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { build } from 'esbuild'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

export async function renderJsx(entryPoint, props = {}) {
  const outputPath = path.resolve('tests', `.render-${process.pid}-${Math.random().toString(16).slice(2)}.cjs`)
  try {
    const result = await build({
      entryPoints: [entryPoint], bundle: true, write: false,
      format: 'cjs', platform: 'node', jsx: 'automatic',
      external: ['react', 'react-dom', 'react/jsx-runtime'],
    })
    fs.writeFileSync(outputPath, result.outputFiles[0].contents)
    const module = createRequire(import.meta.url)(outputPath)
    return renderToStaticMarkup(React.createElement(module.default, props))
  } finally {
    fs.rmSync(outputPath, { force: true })
  }
}
