package tools;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;
import java.util.HexFormat;

public final class RegistrySigner {
    private RegistrySigner() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }

        switch (args[0]) {
            case "generate" -> generate(args);
            case "sign" -> sign(args);
            case "hash" -> hash(args);
            case "verify" -> verify(args);
            default -> {
                usage();
                System.exit(2);
            }
        }
    }

    private static void generate(String[] args) throws Exception {
        if (args.length != 3) {
            usage();
            System.exit(2);
        }

        Path privateKeyFile = Path.of(args[1]);
        Path publicKeyFile = Path.of(args[2]);
        KeyPairGenerator generator = KeyPairGenerator.getInstance("Ed25519");
        KeyPair keyPair = generator.generateKeyPair();

        createParent(privateKeyFile);
        createParent(publicKeyFile);
        Files.writeString(
                privateKeyFile,
                Base64.getEncoder().encodeToString(keyPair.getPrivate().getEncoded()) + System.lineSeparator(),
                StandardCharsets.US_ASCII
        );
        Files.writeString(
                publicKeyFile,
                Base64.getEncoder().encodeToString(keyPair.getPublic().getEncoded()) + System.lineSeparator(),
                StandardCharsets.US_ASCII
        );

        System.out.println("Generated Ed25519 key pair.");
        System.out.println("Keep private: " + privateKeyFile.toAbsolutePath());
        System.out.println("Commit/public: " + publicKeyFile.toAbsolutePath());
        System.out.println("Public key for ModGuardConfig:");
        System.out.println(Base64.getEncoder().encodeToString(keyPair.getPublic().getEncoded()));
    }

    private static void sign(String[] args) throws Exception {
        if (args.length != 4) {
            usage();
            System.exit(2);
        }

        Path privateKeyFile = Path.of(args[1]);
        Path registryFile = Path.of(args[2]);
        Path signatureFile = Path.of(args[3]);

        byte[] privateKeyBytes = Base64.getDecoder().decode(
                Files.readString(privateKeyFile, StandardCharsets.US_ASCII).trim()
        );
        PrivateKey privateKey = KeyFactory.getInstance("Ed25519")
                .generatePrivate(new PKCS8EncodedKeySpec(privateKeyBytes));

        byte[] registryBytes = Files.readAllBytes(registryFile);
        Signature signer = Signature.getInstance("Ed25519");
        signer.initSign(privateKey);
        signer.update(registryBytes);
        byte[] signature = signer.sign();

        createParent(signatureFile);
        Files.writeString(
                signatureFile,
                Base64.getEncoder().encodeToString(signature) + System.lineSeparator(),
                StandardCharsets.US_ASCII
        );
        System.out.println("Signed " + registryFile + " -> " + signatureFile);
    }


    private static void verify(String[] args) throws Exception {
        if (args.length != 4) {
            usage();
            System.exit(2);
        }

        Path publicKeyFile = Path.of(args[1]);
        Path registryFile = Path.of(args[2]);
        Path signatureFile = Path.of(args[3]);

        byte[] publicKeyBytes = Base64.getDecoder().decode(
                Files.readString(publicKeyFile, StandardCharsets.US_ASCII).trim()
        );
        var publicKey = KeyFactory.getInstance("Ed25519")
                .generatePublic(new X509EncodedKeySpec(publicKeyBytes));
        byte[] signatureBytes = Base64.getDecoder().decode(
                Files.readString(signatureFile, StandardCharsets.US_ASCII).trim()
        );

        Signature verifier = Signature.getInstance("Ed25519");
        verifier.initVerify(publicKey);
        verifier.update(Files.readAllBytes(registryFile));
        if (!verifier.verify(signatureBytes)) {
            System.err.println("Signature verification failed");
            System.exit(1);
        }
        System.out.println("Signature verified");
    }

    private static void hash(String[] args) throws Exception {
        if (args.length != 2) {
            usage();
            System.exit(2);
        }

        Path file = Path.of(args[1]);
        byte[] digest = MessageDigest.getInstance("SHA-512").digest(Files.readAllBytes(file));
        System.out.println(HexFormat.of().formatHex(digest));
    }

    private static void createParent(Path file) throws Exception {
        Path parent = file.toAbsolutePath().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
    }

    private static void usage() {
        System.err.println("Usage:");
        System.err.println("  java tools/RegistrySigner.java generate <private-key-file> <public-key-file>");
        System.err.println("  java tools/RegistrySigner.java sign <private-key-file> <registry.json> <registry.sig>");
        System.err.println("  java tools/RegistrySigner.java hash <mod.jar>");
        System.err.println("  java tools/RegistrySigner.java verify <public-key-file> <registry.json> <registry.sig>");
    }
}
