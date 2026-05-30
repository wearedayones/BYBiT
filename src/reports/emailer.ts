import nodemailer from 'nodemailer';
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

export async function sendReport(subject: string, html: string, period: string): Promise<void> {
  const sql = getDb();
  const to = env.REPORT_EMAIL;
  let deliveredOk = false;

  // Skip SMTP when a proxy URL is set — that signals a restricted cloud
  // environment where SMTP ports are blocked. Telegram handles delivery there.
  const smtpEnabled = !!(to && env.REPORT_EMAIL_APP_PASSWORD && !env.BYBIT_PROXY_URL);

  if (smtpEnabled) {
    const transport = createTransport();
    if (transport) {
      try {
        await transport.sendMail({ from: to, to, subject, html });
        deliveredOk = true;
        log.info({ subject, period }, 'Report sent via email');
      } catch (e) {
        log.warn({ subject }, 'SMTP send failed — report stored in DB only');
      }
    }
  }

  await sql`
    INSERT INTO report_history (period, subject, html_body, delivered_to, delivered_ok)
    VALUES (${period}, ${subject}, ${html}, ${to ?? null}, ${deliveredOk})
  `.catch(e => log.error({ e }, 'Failed to save report'));
}
