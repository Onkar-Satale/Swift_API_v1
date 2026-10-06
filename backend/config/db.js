import mongoose from 'mongoose';

/**
 * Connects to MongoDB using connection string from MONGO_URI environment variable.
 * Terminates process immediately on connection failure.
 */
const connectDB = async () => {
  try {
    let uri = (process.env.MONGO_URI || '').trim();
    if (uri.startsWith('MONGO_URI=')) {
      uri = uri.replace('MONGO_URI=', '').trim();
    }
    uri = uri.replace(/^["']|["']$/g, '').trim();

    const conn = await mongoose.connect(uri);
    console.log(`✅ MongoDB connected: ${conn.connection.host}`);
  } catch (err) {
    console.error('❌ MongoDB connection error:', err);
    process.exit(1);
  }
};

export default connectDB;