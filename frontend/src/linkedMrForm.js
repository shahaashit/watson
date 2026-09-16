export async function submitLinkedMr(rawUrl, onAdd, onSuccess) {
  const url = rawUrl.trim()
  if (!url || !onAdd) return false
  if (await onAdd(url) !== true) return false
  onSuccess()
  return true
}
