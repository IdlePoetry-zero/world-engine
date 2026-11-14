import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export function PageEmpty({ eyebrow, title, body, action }: { eyebrow: string; title: string; body: string; action?: ReactNode }) {
  return <section className="page-empty-state page-enter"><div className="empty-page-figure"><div className="figure-ring ring-outer" /><div className="figure-ring ring-inner" /><div className="figure-center"><Icon name="orbit" size={25} /></div><span className="figure-star star-one">✳</span><span className="figure-star star-two">·</span></div><div className="eyebrow"><span className="eyebrow-line" />{eyebrow}</div><h1>{title}</h1><p>{body}</p>{action}</section>
}

export function InlineState({ icon, title, description, action }: { icon: IconName; title: string; description: string; action?: ReactNode }) {
  return <div className="inline-empty"><span className="inline-empty-icon"><Icon name={icon} size={19} /></span><strong>{title}</strong><p>{description}</p>{action}</div>
}
