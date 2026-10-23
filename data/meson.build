i18n = import('i18n')
gnome = import('gnome')

install_data(
  'org.x.clockenstein.gschema.xml',
  install_dir: join_paths(get_option('datadir'), 'glib-2.0', 'schemas'),
)

install_data(
  'clockenstein-calendar.svg',
  'clockenstein-clocks.svg',
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)

gnome.post_install(glib_compile_schemas: true, gtk_update_icon_cache: true)

i18n.merge_file(
  input: 'org.x.clockenstein.Calendar.desktop.in',
  output: 'org.x.clockenstein.Calendar.desktop',
  type: 'desktop',
  po_dir: join_paths(meson.project_source_root(), 'po'),
  install: true,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

i18n.merge_file(
  input: 'org.x.clockenstein.Clocks.desktop.in',
  output: 'org.x.clockenstein.Clocks.desktop',
  type: 'desktop',
  po_dir: join_paths(meson.project_source_root(), 'po'),
  install: true,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

i18n.merge_file(
  input: 'org.x.clockenstein.Calendar.metainfo.xml.in',
  output: 'org.x.clockenstein.Calendar.metainfo.xml',
  type: 'xml',
  po_dir: join_paths(meson.project_source_root(), 'po'),
  install: true,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)

i18n.merge_file(
  input: 'org.x.clockenstein.Clocks.metainfo.xml.in',
  output: 'org.x.clockenstein.Clocks.metainfo.xml',
  type: 'xml',
  po_dir: join_paths(meson.project_source_root(), 'po'),
  install: true,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)
