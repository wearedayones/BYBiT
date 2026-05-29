import nodemailer from 'nodemailer';
import { env } from '../config/env';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';

const log = childLogger({ module: 'emailer' });

function createTransport() {
  if (!env.REPORT_EMAIL || !env.REPORT_EMAIL_APP_PASSWORD) return null;
  return nodemailer.createTransport({
    host: 'smtp.gmail.com',
    port: 587,
    secure: false,
    auth: { user: env.REPORT_EMAIL, pass: env.REPORT_EMAIL_APP_PASSWORD },
  });
}

export async function sendReport(subject: string, html: string, period: string): Promise<void> {
  const sql = getDb();
  const to = env.REPORT_EMAIL;
  let deliveredOk = false;

  if (to && env.REPORT_EMAIL_APP_PASSWORD) {
    const transport = createTransport();
    if (transport) {
      try {
        await transport.sendMail({ from: to, to, subject, html });
        deliveredOk = true;
        log.info({ subject, period }, 'Report sent');
      } catch (e) {
        log.error({ e }, 'Failed to send email report');
      }
    }
  } else {
    log.info({ subject }, 'Email not configured — report stored in DB only');
  }

  await sql`
    INSERT INTO report_history (period, subject, html_body, delivered_to, delivered_ok)
    VALUES (${period}, ${subject}, ${html}, ${to ?? null}, ${deliveredOk})
  `.catch(e => log.error({ e }, 'Failed to save report'));
}
