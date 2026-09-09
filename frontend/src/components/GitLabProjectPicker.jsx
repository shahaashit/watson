export default function GitLabProjectPicker({
  projects = [],
  selectedIds = [],
  query = '',
  onQueryChange,
  onSearch,
  onToggle,
  onSave,
  busy = false,
  message = '',
}) {
  const needle = query.trim().toLocaleLowerCase()
  const visible = needle
    ? projects.filter((project) => `${project.name} ${project.path}`.toLocaleLowerCase().includes(needle))
    : projects
  const selected = new Set(selectedIds)

  return <section className="gitlab-project-settings" aria-labelledby="gitlab-project-settings-title">
    <div className="gitlab-project-heading">
      <div><h4 id="gitlab-project-settings-title">Tracked repositories</h4><p>Only selected repositories are discovered automatically. Manually imported MR links remain available.</p></div>
      <span>{selected.size} selected</span>
    </div>
    <div className="gitlab-project-search-row"><label className="gitlab-project-search">Search repositories<input type="search" value={query} onChange={(event) => onQueryChange?.(event.target.value)} placeholder="Search by name or full path" disabled={Boolean(busy)} /></label><button type="button" onClick={onSearch} disabled={Boolean(busy) || !query.trim()}>{busy === 'search' ? 'Searching…' : 'Search GitLab'}</button></div>
    <div className="gitlab-project-list" aria-label="GitLab repositories">
      {visible.map((project) => <label className="gitlab-project-option" key={project.id}>
        <input type="checkbox" value={project.id} checked={selected.has(project.id)} onChange={() => onToggle?.(project.id)} disabled={Boolean(busy)} />
        <span><strong>{project.name}</strong><small>{project.path}</small></span>
      </label>)}
      {!visible.length && <p className="settings-muted">No repositories match this search.</p>}
    </div>
    <div className="gitlab-project-footer">
      <button className="settings-primary" type="button" onClick={onSave} disabled={Boolean(busy)}>{busy === 'save' ? 'Saving and syncing…' : 'Save repositories'}</button>
      {message && <p className={message.includes('could not') ? 'settings-error' : 'settings-success'} role="status">{message}</p>}
    </div>
  </section>
}
