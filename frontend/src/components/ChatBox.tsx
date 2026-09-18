import React, { useState, useRef, useEffect } from 'react';
import { Send, Cpu, LayoutList, CheckCircle2 } from 'lucide-react';
import '../App.css';

interface Chunk {
  id: number;
  content: string;
  type: string;
}

interface Message {
  role: 'user' | 'ai';
  content?: string;
  chunks?: Chunk[];
}

export const ChatBox = ({ type = 'onboarding' }: { type: 'onboarding' | 'search' }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const endOfMessagesRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    endOfMessagesRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;

    const userMsg = input;
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMsg }, { role: 'ai', chunks: [] }]);
    setIsStreaming(true);

    try {
      const endpoint = type === 'search' ? '/api/v1/ai/chat' : '/api/v1/onboarding/chat';
      const response = await fetch(`http://localhost:8000${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userMsg })
      });

      if (!response.body) throw new Error('No readable stream');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        const chunkStr = decoder.decode(value, { stream: true });
        const lines = chunkStr.split('\n');
        
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = JSON.parse(line.substring(6)) as Chunk;
            
            setMessages(prev => {
              const newMsgs = [...prev];
              const lastMsg = newMsgs[newMsgs.length - 1];
              if (lastMsg.role === 'ai') {
                lastMsg.chunks = [...(lastMsg.chunks || []), data];
              }
              return newMsgs;
            });
          }
        }
      }
    } catch (error) {
      console.error('Streaming error:', error);
    } finally {
      setIsStreaming(false);
    }
  };

  const renderAiMessage = (msg: Message) => {
    if (!msg.chunks || msg.chunks.length === 0) return <div className="thinking-pulse">Agent is thinking...</div>;

    const steps = msg.chunks.filter(c => c.type === 'steps');
    const thinking = msg.chunks.filter(c => c.type === 'thinking');
    const textChunks = msg.chunks.filter(c => c.type === 'text' || c.type === 'response');
    const finalText = textChunks.map(c => c.content).join(' ');

    return (
      <div className="ai-message-content">
        {thinking.length > 0 && (
           <div className="step-item thinking">
              <Cpu size={16} className="thinking-pulse" />
              <span>{thinking[thinking.length - 1].content}</span>
           </div>
        )}
        
        {steps.map((step, idx) => (
          <div key={idx} className="step-item">
            <CheckCircle2 size={16} className="text-green-400" />
            <span>{step.content}</span>
          </div>
        ))}
        
        {finalText && (
          <div className="final-response" style={{ marginTop: '0.75rem' }}>
            {finalText}
          </div>
        )}
      </div>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="chat-container">
        {messages.map((msg, idx) => (
          <div key={idx} className={`message-row ${msg.role}`}>
            <div className="message-bubble">
              {msg.role === 'user' ? msg.content : renderAiMessage(msg)}
            </div>
          </div>
        ))}
        <div ref={endOfMessagesRef} />
      </div>

      <div className="input-area glass-panel">
        <form onSubmit={handleSubmit} className="input-box">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type your message..."
            disabled={isStreaming}
          />
          <button type="submit" className="send-button" disabled={isStreaming || !input.trim()}>
            <Send size={18} />
          </button>
        </form>
      </div>
    </div>
  );
};
