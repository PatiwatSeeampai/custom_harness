export default [
    {
        languageOptions: {
            globals: {
                console: "readonly",
                process: "readonly",
                module: "readonly",
                require: "readonly",
                __dirname: "readonly",
                window: "readonly",
                document: "readonly",
                setTimeout: "readonly",
                clearTimeout: "readonly",
                setInterval: "readonly",
                clearInterval: "readonly"
            }
        },
        rules: {
            "no-unused-vars": "warn",
            "no-undef": "error"
        }
    }
];
