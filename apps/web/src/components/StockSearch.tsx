'use client';

import { useRouter } from 'next/navigation';
import { useId, useState, type FormEvent } from 'react';

// Letters, digits, '.' and '-' (BRK.B, RDS-A). The server's own symbol pattern allows 16.
const SYMBOL = /^[A-Z0-9.-]{1,16}$/;

/** "Check a stock": type a ticker, press Enter, land on its page. */
export function StockSearch() {
  const router = useRouter();
  const inputId = useId();
  const hintId = useId();
  const [value, setValue] = useState('');
  const [invalid, setInvalid] = useState(false);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const symbol = value.trim().toUpperCase();
    if (symbol === '') return;
    if (!SYMBOL.test(symbol)) {
      setInvalid(true);
      return;
    }
    setValue('');
    router.push(`/stocks/${encodeURIComponent(symbol)}`);
  }

  return (
    <form onSubmit={submit} role="search" noValidate className="relative">
      <label htmlFor={inputId} className="sr-only">
        Check a stock
      </label>
      <input
        id={inputId}
        type="text"
        value={value}
        onChange={(e) => {
          setValue(e.target.value.toUpperCase());
          setInvalid(false);
        }}
        placeholder="Check a stock"
        autoComplete="off"
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        enterKeyHint="go"
        aria-invalid={invalid}
        aria-describedby={invalid ? hintId : undefined}
        className={`w-36 rounded border bg-bg-subtle px-2.5 py-1.5 font-mono text-sm text-ink placeholder:font-sans placeholder:text-ink-subtle focus:border-accent focus:outline-none md:w-44 ${
          invalid ? 'border-bear' : 'border-line'
        }`}
      />
      {invalid && (
        <p
          id={hintId}
          role="alert"
          className="absolute right-0 top-full z-20 mt-1 whitespace-nowrap text-xs text-bear"
        >
          Use letters, digits, . or - only
        </p>
      )}
    </form>
  );
}
