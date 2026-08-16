import base64
import os
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('SECRET_NAME', 'test')

import backend_lambda


class ComposeSendTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            'email': 'sender@example.com',
            'provider': 'gmail',
            'access_token': 'token',
        }
        self.message = {
            'to': ['to@example.com'],
            'cc': ['cc@example.com'],
            'bcc': ['bcc@example.com'],
            'subject': 'Status update',
            'body': 'The project is on schedule.',
            'replyTo': 'replies@example.com',
            'threadId': 'thread-1',
            'inReplyTo': '<message-1@example.com>',
            'mode': 'reply',
            'originalMessageId': 'provider-message-1',
        }
        self.attachments = [{
            'filename': 'notes.txt',
            'mimeType': 'text/plain',
            'content': b'attachment body',
        }]

    def test_gmail_send_builds_threaded_mime_message(self):
        with patch.object(backend_lambda, 'api_request', return_value={'id': 'sent-1'}) as request:
            backend_lambda._send_gmail_message('user-1', self.account, self.message, self.attachments)

        payload = request.call_args.kwargs['payload']
        padded = payload['raw'] + '=' * (-len(payload['raw']) % 4)
        parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))

        self.assertEqual(payload['threadId'], 'thread-1')
        self.assertEqual(parsed['To'], 'to@example.com')
        self.assertEqual(parsed['Cc'], 'cc@example.com')
        self.assertEqual(parsed['Bcc'], 'bcc@example.com')
        self.assertEqual(parsed['Reply-To'], 'replies@example.com')
        self.assertEqual(parsed['In-Reply-To'], '<message-1@example.com>')
        self.assertEqual(parsed['References'], '<message-1@example.com>')
        self.assertEqual(parsed['Subject'], 'Status update')
        self.assertIn('The project is on schedule.', parsed.get_body(preferencelist=('plain',)).get_content())
        self.assertEqual([part.get_filename() for part in parsed.iter_attachments()], ['notes.txt'])

    def test_outlook_reply_all_updates_and_sends_provider_draft(self):
        calls = []

        def fake_request(user_id, account, url, method='POST', payload=None):
            calls.append({'url': url, 'method': method, 'payload': payload})
            return {'id': 'draft id'} if url.endswith('/createReplyAll') else None

        outlook_account = {**self.account, 'provider': 'outlook'}
        reply_all = {**self.message, 'mode': 'replyAll', 'originalMessageId': 'original/id'}
        with patch.object(backend_lambda, 'api_request', side_effect=fake_request):
            backend_lambda._send_outlook_message('user-1', outlook_account, reply_all, self.attachments)

        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0]['url'].endswith('/messages/original%2Fid/createReplyAll'))
        self.assertEqual(calls[1]['method'], 'PATCH')
        self.assertTrue(calls[1]['url'].endswith('/messages/draft%20id'))
        self.assertEqual(calls[1]['payload']['toRecipients'][0]['emailAddress']['address'], 'to@example.com')
        self.assertTrue(calls[2]['url'].endswith('/messages/draft%20id/attachments'))
        self.assertEqual(calls[2]['payload']['name'], 'notes.txt')
        self.assertTrue(calls[3]['url'].endswith('/messages/draft%20id/send'))

    def test_attachment_count_is_limited(self):
        attachments = [{'filename': f'{index}.txt', 'content': ''} for index in range(11)]
        with self.assertRaisesRegex(ValueError, 'maximum of 10'):
            backend_lambda._decode_compose_attachments(attachments)


if __name__ == '__main__':
    unittest.main()