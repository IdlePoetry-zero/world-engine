import type { SVGProps } from 'react'

export type IconName =
  | 'activity' | 'alert' | 'archive' | 'arrow-right' | 'arrow-up' | 'arrow-up-right' | 'book' | 'box' | 'brain'
  | 'branch' | 'check' | 'chevron' | 'chevrons' | 'clock' | 'close' | 'cloud' | 'cloud-off' | 'compass'
  | 'copy' | 'cpu' | 'crosshair' | 'download' | 'edit' | 'eye' | 'fingerprint' | 'info' | 'layers' | 'list'
  | 'lock' | 'menu' | 'message' | 'pin' | 'plug' | 'plus' | 'pulse' | 'refresh' | 'route' | 'server'
  | 'shield' | 'sliders' | 'spark' | 'spinner' | 'timeline' | 'users' | 'user' | 'orbit'

const paths: Record<IconName, string | string[]> = {
  activity: ['M3 12h4l3-8 4 16 3-8h4'],
  alert: ['M12 9v4', 'M12 17h.01', 'M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z'],
  archive: ['M3 4h18v4H3z', 'M5 8v12h14V8', 'M10 12h4'],
  'arrow-right': 'M5 12h14m-7-7 7 7-7 7',
  'arrow-up': 'M12 19V5m-7 7 7-7 7 7',
  'arrow-up-right': 'M7 17 17 7M7 7h10v10',
  book: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z'],
  box: ['M21 8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7Z', 'm3.3 7 8.7 5 8.7-5', 'M12 22V12'],
  brain: ['M12 18V5', 'M15.5 14a4.5 4.5 0 1 1 0-9 3.5 3.5 0 0 1 0 7h-7a4.5 4.5 0 1 0 0 9h7a3.5 3.5 0 0 0 0-7'],
  branch: ['M6 3v12', 'M18 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z', 'M6 18a3 3 0 1 1 0-6 3 3 0 0 1 0 6Z', 'M18 9h-3a9 9 0 0 0-9 9'],
  check: 'm5 12 4 4L19 6',
  chevron: 'm9 18 6-6-6-6',
  chevrons: ['m7 15 5 5 5-5', 'm7 9 5-5 5 5'],
  clock: ['M12 8v4l3 2', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z'],
  close: 'M18 6 6 18M6 6l12 12',
  cloud: ['M20 16.2A4.5 4.5 0 0 0 18 7.8a6 6 0 0 0-11.7 1.5A4 4 0 0 0 6 17h12'],
  'cloud-off': ['m2 2 20 20', 'M5.2 5.2A6 6 0 0 1 17.8 8', 'M19 13a4.5 4.5 0 0 1 0 8H7a4 4 0 0 1-3.5-5.9'],
  compass: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z', 'm16.2 7.8-2.7 5.7-5.7 2.7 2.7-5.7 5.7-2.7Z'],
  copy: ['M8 8V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4', 'M4 6h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z'],
  cpu: ['M12 8v8', 'M8 12h8', 'M5 3v2', 'M19 3v2', 'M5 19v2', 'M19 19v2', 'M3 5h2', 'M3 19h2', 'M19 5h2', 'M19 19h2', 'M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z'],
  crosshair: ['M12 2v4', 'M12 18v4', 'M2 12h4', 'M18 12h4', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z'],
  download: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5', 'M12 15V3'],
  edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z'],
  eye: ['M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z'],
  fingerprint: ['M12 11a2 2 0 0 0-2 2c0 3-.5 5.5-1.5 7', 'M14 13c0 3 .4 5.2 1.2 6.7', 'M8 14c0 2-.3 3.8-.9 5.2', 'M16 11a4 4 0 0 0-8 0', 'M12 3a8 8 0 0 0-8 8', 'M20 11a8 8 0 0 0-2.4-5.7'],
  info: ['M12 11v5', 'M12 8h.01', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z'],
  layers: ['m12 2 10 5-10 5L2 7l10-5Z', 'm2 12 10 5 10-5', 'm2 17 10 5 10-5'],
  list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
  lock: ['M5 11h14v10H5z', 'M8 11V7a4 4 0 1 1 8 0v4'],
  menu: ['M4 6h16', 'M4 12h16', 'M4 18h16'],
  message: ['M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z'],
  pin: ['M12 17v5', 'M5 3h14l-2 7 3 3v2H4v-2l3-3-2-7Z'],
  plug: ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M5 8h14v4a7 7 0 0 1-14 0V8Z'],
  plus: 'M12 5v14m-7-7h14',
  pulse: ['M3 12h4l3-8 4 16 3-8h4'],
  refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M5.5 9A7 7 0 0 1 18 6l2 2', 'M6 16l-2 2 2 2', 'M18.5 15A7 7 0 0 1 6 18'],
  route: ['M4 6h16', 'M4 18h16', 'M7 6v12', 'M17 6v12', 'M7 11h10'],
  server: ['M4 4h16v6H4z', 'M4 14h16v6H4z', 'M8 7h.01', 'M8 17h.01', 'M12 7h5', 'M12 17h5'],
  shield: ['M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z', 'm9 12 2 2 4-4'],
  sliders: ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3', 'M2 14h4', 'M10 8h4', 'M18 16h4'],
  spark: ['m12 3 1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2L12 3Z', 'm19 14 1 2.5 2.5 1-2.5 1-1 2.5-1-2.5-2.5-1 2.5-1 1-2.5Z'],
  spinner: ['M12 2v4', 'M12 18v4', 'm4.9 4.9 2.8 2.8', 'm16.3 16.3 2.8 2.8', 'M2 12h4', 'M18 12h4', 'm4.9 19.1 2.8-2.8', 'm16.3 7.7 2.8-2.8'],
  timeline: ['M3 12h3', 'M8 12h4', 'M15 12h6', 'M6 8v8', 'M14 8v8', 'M3 4h18', 'M3 20h18'],
  users: ['M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2', 'M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M20 21v-2a4 4 0 0 0-3-3.9', 'M16 3.1a4 4 0 0 1 0 7.8'],
  user: ['M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2', 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z'],
  orbit: ['M12 12m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0', 'M12 2a10 10 0 1 0 10 10', 'M12 5a7 7 0 1 0 7 7', 'M12 8a4 4 0 1 0 4 4'],
}

type Props = SVGProps<SVGSVGElement> & { name: IconName; size?: number }

export function Icon({ name, size = 18, ...props }: Props) {
  const shape = paths[name]
  const list = Array.isArray(shape) ? shape : [shape]
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      {list.map((path, index) => <path key={`${name}-${index}`} d={path} />)}
    </svg>
  )
}
