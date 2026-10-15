import Markdown from 'react-markdown'

/** Markdown rendering without raw HTML and without remote images (the CSP blocks them anyway). */
export function MarkdownView({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown
        skipHtml
        disallowedElements={['img']}
        unwrapDisallowed
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  )
}
