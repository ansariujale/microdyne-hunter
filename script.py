import sys
content = open('modules/email_queue.py', 'r', encoding='utf-8').read()
old_code = '''            # Support both SSL (port 465) and TLS (port 587)
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)'''

new_code = '''            # Support both SSL (port 465) and TLS (port 587)
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)

            # IMAP Append to Sent folder (so it shows up in Hostinger Webmail/Outlook)
            try:
                import imaplib, time
                imap_host = smtp_host.replace('smtp', 'imap')
                mail = imaplib.IMAP4_SSL(imap_host, 993)
                mail.login(smtp_user, smtp_password)
                status, folders = mail.list()
                sent_folder = 'INBOX.Sent'
                if status == 'OK':
                    for f in folders:
                        f_str = f.decode()
                        if 'sent' in f_str.lower():
                            sent_folder = f_str.split(' "/" ')[-1].strip('"')
                            break
                mail.append(sent_folder, '\\\\Seen', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                mail.logout()
                logger.info(f'[Email] Appended message to IMAP Sent folder ({sent_folder})')
            except Exception as imap_e:
                logger.warning(f'[Email] Failed to append to IMAP Sent folder: {imap_e}')'''

content = content.replace(old_code, new_code)
open('modules/email_queue.py', 'w', encoding='utf-8').write(content)
