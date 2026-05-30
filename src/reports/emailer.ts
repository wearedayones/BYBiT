import nodemailer from 'nodemailer';
import { request } from 'undici';
import { env } from '../config/env';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';

const log = childLogger({ module: 'emailer' });

function createTransport() {
  if (!env.REPORT_EMAIL || !env.REPORT_EMAIL_APP_PASSWORD) return null;
  const host = (env as Record<string, string | undefined>).SMTP_HOST ?? 'smtp.gmail.com';
  const port = parseInt((env as Record<string, string | undefined>).SMTP_PORT ?? '587', 10);
  return nodemailer.createTransport({
    host, port,
    secure: port === 465,
    auth: { user: env.REPORT_EMAIL, pass: env.REPORT_EMAIL_APP_PASSWORD },
    connectionTimeout: 8_000,
    greetingTimeout: 8_000,
    socketTimeout: 10_000,
  });
}

// In cloud environments where SMTP ports (587/465) are blocked, relay the
// send through the Cloudflare Worker over HTTPS (443) — same trick as the
// Bybit proxy. The Worker opens the actual SMTP socket to Gmail on our behalf.
async function sendViaProxy(to: string, subject: string, html: string): Promise<void> {
  const url = `${env.BYBIT_PROXY_URL}/sendmail`;
  const res = await request(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      to,
      subject,
      html,
      user: env.REPORT_EMAIL,
      pass: env.REPORT_EMAIL_APP_PASSWORD,
    }),
    headersTimeout: 20_000,
    bodyTimeout: 20_000,
  });
  const body = await res.body.json() as { ok?: boolean; error?: string };
  if (res.statusCode !== 200 || !body.ok) {
    throw new Error(`Mail relay failed (${res.statusCode}): ${body.error ?? 'unknown'}`);
  }
}

export async function sendReport(subject: string, html: string, period: string): Promise<void> {
  const sql = getDb();
  const to = env.REPORT_EMAIL;
  let deliveredOk = false;

  if (to && env.REPORT_EMAIL_APP_PASSWORD) {
    try {
      if (env.BYBIT_PROXY_URL) {
        // Cloud path: SMTP ports are blocked, relay through the Worker over 443.
        await sendViaProxy(to, subject, html);
      } else {
        // Direct SMTP (local / unrestricted environments).
        const transport = createTransport();
        if (!transport) throw new Error('No transport configured');
        await transport.sendMail({ from: to, to, subject, html });
      }
      deliveredOk = true;
      log.info({ subject, period, via: env.BYBIT_PROXY_URL ? 'worker' : 'smtp' }, 'Report sent');
    } catch (e) {
      log.error({ e }, 'Failed to send email report');
    }
  } else {
    log.info({ subject }, 'Email not configured — report stored in DB only');
  }

  await sql`
    INSERT INTO report_history (period, subject, html_body, delivered_to, delivered_ok)
    VALUES (${period}, ${subject}, ${html}, ${to ?? null}, ${deliveredOk})
  `.catch(e => log.error({ e }, 'Failed to save report'));
}
